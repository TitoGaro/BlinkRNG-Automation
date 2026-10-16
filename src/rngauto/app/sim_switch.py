"""Switch simulada: existe para los tests y para ``rngauto simulate`` (la interfaz gráfica ya no ofrece
modo demo).

Reúne en un objeto lo que en real son tres cosas separadas: la imagen que llega por la capturadora
(``read``), el mando (``controller``) y el propio juego (RNG + escena + encuentro). Los parámetros
«verdaderos» (periodo, delays, latencias) son desconocidos para la estrategia, que debe deducirlos o
calibrarlos igual que con hardware real. Todo avanza con un ``SimulatedClock``: no hay esperas reales.
"""
from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

from rngauto.control.controller import Button, Command, Controller, ControllerStatus, DPad
from rngauto.core.clock import SimulatedClock
from rngauto.core.encounter import SceneSpec, TimelinePhase
from rngauto.core.pokemon import PokemonTraits
from rngauto.games.bdsp.encounters import StaticTargetFinder
from rngauto.games.bdsp.identify import Observation, observe_like
from rngauto.games.bdsp.planner import simulate_phase
from rngauto.games.bdsp.scene import PLAYER, SceneModel, SceneSimulator
from rngauto.games.bdsp.xorshift import XorshiftState
from rngauto.vision.frames import Frame, SourceInfo
from rngauto.vision.synthetic import GroundTruthBlink, SyntheticEyeScene, SyntheticRenderer, openness_at


@dataclass(frozen=True)
class SimTruth:
    """Parámetros reales del juego simulado (la estrategia no los conoce)."""

    period_s: float = 1.0197
    idle: SceneSpec = SceneSpec()
    phase: TimelinePhase = TimelinePhase("entrada", "A", blank_reads=2, pause_s=2.0, scene=SceneSpec(pokemon_npc=1))
    final_delay: int = 86  # el valor de partida habitual es 84: la calibración debe descubrir el +2
    input_latency_s: float = 0.045  # ESP32 → Bluetooth → juego
    video_latency_s: float = 0.080  # juego → capturadora → PC
    # Menú y Pokédex (avance rápido). Valores inventados para la simulación: la estrategia los mide.
    menu_cursor: int = 2  # posición inicial del cursor del menú (0 Pokédex … 4 Mapa … 7 Regalo; 2 = Mochila)
    map_exit_to_menu: bool = True  # False: salir del mapa vuelve al mundo abierto (para probar la seguridad)
    pokedex_open_calls: tuple[int, int] = (110, 170)
    pokedex_entry_calls: tuple[int, int] = (4, 12)
    pokedex_scroll_calls_per_s: tuple[float, float] = (100.0, 140.0)
    menu_close_calls: int = 1  # cerrar el menú consume una llamada extra al RNG
    # Flujo «fuera de la cueva» (p. ej. Uxie): mensaje de Serbal con la caña, ruta y legendario delante.
    message_scene: bool = False
    item_direction: str = "LEFT"  # dirección de la caña en la rueda de «+»
    door_s: float = 2.5  # transición al entrar por la puerta
    steps_to_legendary: int = 10
    walk_steps_per_s: float = 4.0


# Pantalla en la que está el juego simulado (``_Game.menu``).
MENU_CLOSED, MENU_OPEN, POKEDEX, POKEDEX_ENTRY, TOWN_MAP, OTHER_SCREEN = 0, 1, 2, 3, 4, 9
MENU_POKEDEX, MENU_TOWN_MAP = 0, 4  # cuadrícula 2×4 del menú de BDSP (fila 1: 0-3, fila 2: 4-7)


@dataclass
class _Game:
    """Estado de una partida simulada; ``SimulatedSwitch.reset_game`` crea otra."""

    base: XorshiftState
    t0: float
    idle: SceneSimulator
    blinks: list[GroundTruthBlink] = field(default_factory=list)
    phase: str = "idle"  # idle | timeline | battle
    timeline: SceneSimulator | None = None
    generated: PokemonTraits | None = None
    generated_advance: int | None = None
    trigger_count: int | None = None
    menu: int = MENU_CLOSED
    cursor: int = 0
    message: bool = False  # mensaje de Serbal en pantalla
    wheel: bool = False  # rueda de objetos registrados abierta
    location: str = "start"  # start | door | cave
    door_until: float = 0.0
    cave_steps: float = 0.0


class SimulatedSwitch:
    """Juego, capturadora y consola simulados (ver docstring del módulo).

    Las pulsaciones llegan por ``SimController`` con la latencia de entrada y se aplican al juego cuando
    ``read`` alcanza su instante; la imagen se genera con la latencia de vídeo.
    """

    def __init__(
        self,
        finder: StaticTargetFinder,
        truth: SimTruth = SimTruth(),
        scene: SyntheticEyeScene = SyntheticEyeScene(fps=30, noise_sigma=2, jpeg_quality=80),
        seed: int = 0,
        clock: SimulatedClock | None = None,
    ) -> None:
        self.finder = finder
        self.truth = truth
        self.scene = scene
        self.clock = clock or SimulatedClock(10.0)
        self.rnd = random.Random(seed)
        self.renderer = SyntheticRenderer(scene, seed)
        self.inputs: list[tuple[float, Button]] = []  # (llegada al juego, botón)
        self.holds: list[tuple[float, DPad, float]] = []  # (llegada, dirección, duración) de la cruceta
        self.sticks: list[tuple[float, tuple[float, float]]] = []  # (llegada, stick derecho) inclinado
        self.wrong_talk = 0  # A para hablar sin estar delante del legendario
        self.player_moved = False  # se pulsó la cruceta fuera de los menús (nunca debería pasar)
        self.frame_index = 0
        self.resets = 0
        self._new_game()

    # --- Juego ------------------------------------------------------------------------------------
    def _new_game(self) -> None:
        state = XorshiftState(*(self.rnd.getrandbits(32) for _ in range(4)))
        t0 = self.clock.now()
        model = SceneModel(npc=self.truth.idle.npc, period_s=self.truth.period_s,
                           player_phase=self.truth.idle.player_phase)
        idle = SceneSimulator.start(state, model, start_time_s=t0, first_roll_s=t0 + self.rnd.uniform(0.1, 1.0))
        # Al empezar, el jugador ya ha dejado el mensaje en pantalla; tras un reinicio, no (lo pone el programa).
        self.game = _Game(state, t0, idle, cursor=self.truth.menu_cursor,
                          message=self.truth.message_scene and self.resets == 0)

    def reset_game(self) -> None:
        """Equivale a cerrar el juego y volver a cargar la partida: RNG nuevo.

        Avanza el reloj 25 s (lo que tarda el reinicio) y descarta las entradas que ya habían llegado.
        """
        self.resets += 1
        self.clock.advance(25.0)
        self.inputs = [(t, b) for t, b in self.inputs if t > self.clock.now()]
        self.holds = []
        self.sticks = []
        self._new_game()

    def _outside_input(self, t_in: float, button: Button | None, hold: tuple[DPad, float] | None,
                       stick: tuple[float, float] | None) -> bool:
        """Mensaje de Serbal, rueda de objetos registrados, ruta hasta el legendario (solo ``message_scene``)."""
        g, tr = self.game, self.truth
        if not tr.message_scene or g.phase != "idle" or g.menu != MENU_CLOSED:
            return False
        if g.location == "door" and t_in >= g.door_until:
            g.location = "cave"
        if g.location == "door":
            return True  # durante la transición no se atiende nada
        if stick is not None:
            if g.wheel:
                g.wheel = False
                x, y = stick
                chosen = "UP" if y > 0.5 else "DOWN" if y < -0.5 else "RIGHT" if x > 0.5 else "LEFT" if x < -0.5 else ""
                if chosen == tr.item_direction and g.location == "start":
                    g.message = True  # «¡Hay un momento y un lugar para todo!»
            return True
        if g.message:
            if button is not None and button & (Button.A | Button.B):
                g.message = False
            return True  # con el mensaje, el resto de entradas no hacen nada
        if hold is not None and g.wheel:  # rueda de objetos registrados: se elige con la cruceta
            g.wheel = False
            if hold[0].name == tr.item_direction and g.location == "start":
                g.message = True  # «¡Cada cosa en su momento!»
            return True
        if hold is not None:
            direction, seconds = hold
            if direction is not DPad.UP:
                self.player_moved = True
            elif g.location == "start":
                g.location, g.door_until = "door", t_in + tr.door_s
            elif g.location == "cave":
                g.cave_steps = min(tr.steps_to_legendary, g.cave_steps + seconds * tr.walk_steps_per_s)
            return True
        if button is not None and button & Button.PLUS and not g.wheel:
            g.wheel = True
            return True
        if button is not None and button & Button.A:
            if g.location == "cave" and g.cave_steps >= tr.steps_to_legendary:
                return False  # delante del legendario: la A inicia el timeline (lógica normal)
            if g.location != "start":
                self.wrong_talk += 1
            return True
        return False

    def _menu_input(self, t_in: float, button: Button | None, hold: tuple[DPad, float] | None) -> bool:
        """Menú/Pokédex en la escena de espera. Devuelve True si la entrada la ha consumido el menú."""
        g, tr = self.game, self.truth
        if g.phase != "idle":
            return False
        for ev in g.idle.run_until(t_in):
            self._record_blink(ev)
        if tr.message_scene and g.menu == MENU_CLOSED and (g.message or g.wheel or g.location != "start"):
            return False  # lo atiende ``_outside_input``
        if tr.message_scene and g.menu == MENU_CLOSED and button is not None and button & Button.PLUS:
            return False  # «+» fuera del menú: objetos registrados
        if hold is not None:
            direction, seconds = hold
            if g.menu in (POKEDEX, POKEDEX_ENTRY) and direction in (DPad.UP, DPad.DOWN):
                g.idle.skip(int(self.rnd.uniform(*tr.pokedex_scroll_calls_per_s) * seconds))
            elif g.menu == MENU_OPEN:  # mover el cursor (con vuelta, como en el juego)
                row, col = divmod(g.cursor, 4)
                if direction in (DPad.UP, DPad.DOWN):
                    row = 1 - row
                elif direction is DPad.LEFT:
                    col = (col - 1) % 4
                elif direction is DPad.RIGHT:
                    col = (col + 1) % 4
                g.cursor = row * 4 + col
            elif g.menu == MENU_CLOSED:
                return False  # en el mundo abierto la cruceta movería al personaje
            return True
        if button is None:  # solo stick (p. ej. el toque para mantener despierta la consola)
            return False
        if button & Button.X and g.menu == MENU_CLOSED:
            g.menu = MENU_OPEN
            return True
        if g.menu == MENU_CLOSED:
            return False
        if button & Button.PLUS and g.menu == MENU_OPEN:  # «+ Ver mapa»
            g.menu, g.cursor = TOWN_MAP, MENU_TOWN_MAP
        elif button & Button.A:
            if g.menu == MENU_OPEN:
                if g.cursor == MENU_POKEDEX:
                    g.menu = POKEDEX
                    g.idle.skip(self.rnd.randint(*tr.pokedex_open_calls))
                elif g.cursor == MENU_TOWN_MAP:
                    g.menu = TOWN_MAP
                else:
                    g.menu = OTHER_SCREEN
            elif g.menu == POKEDEX:
                g.menu = POKEDEX_ENTRY
                g.idle.skip(self.rnd.randint(*tr.pokedex_entry_calls))
        elif button & Button.B:
            if g.menu == POKEDEX_ENTRY:
                g.menu = POKEDEX
            elif g.menu == TOWN_MAP:
                g.menu = MENU_OPEN if tr.map_exit_to_menu else MENU_CLOSED
            elif g.menu in (POKEDEX, OTHER_SCREEN):
                g.menu = MENU_OPEN
            elif g.menu == MENU_OPEN:
                g.menu = MENU_CLOSED
                g.idle.skip(tr.menu_close_calls)
        return True

    def _advance_game(self, t_game: float) -> None:
        """Aplica en orden cronológico las entradas que han llegado al juego hasta ``t_game``.

        Cada entrada la atiende el primero que la reconozca: menú, escena exterior o lógica del encuentro
        (A en la escena de espera inicia el timeline; A en el timeline genera el Pokémon).
        """
        g = self.game
        while True:
            pending = sorted([(t, b, None, None) for t, b in self.inputs if t <= t_game]
                             + [(t, None, (d, sec), None) for t, d, sec in self.holds if t <= t_game]
                             + [(t, None, None, st) for t, st in self.sticks if t <= t_game],
                             key=lambda x: x[0])
            if not pending:
                break
            t_in, button, hold, stick = pending[0]
            if stick is not None:
                self.sticks.remove((t_in, stick))
            elif hold is None:
                self.inputs.remove((t_in, button))
            else:
                self.holds.remove((t_in, hold[0], hold[1]))
            if self._menu_input(t_in, button, hold) or self._outside_input(t_in, button, hold, stick):
                continue
            if stick is not None:
                continue
            if button is None:
                if g.phase == "idle":
                    self.player_moved = True  # cruceta en el mundo abierto: el personaje se habría movido
                continue
            if g.phase == "idle" and button & Button.A:
                for ev in g.idle.run_until(t_in):
                    self._record_blink(ev)
                g.trigger_count = g.idle.advance
                g.timeline = simulate_phase(g.base, g.idle.advance, t_in, self.truth.phase, self.truth.period_s)
                g.phase = "timeline"
            elif g.phase == "timeline" and button & Button.A:
                g.timeline.run_until(t_in)
                g.generated_advance = g.timeline.advance + self.truth.final_delay
                g.generated = self.finder.generate(g.base, g.generated_advance)
                g.phase = "battle"
        if g.phase == "idle":
            for ev in g.idle.run_until(t_game):
                self._record_blink(ev)

    def _record_blink(self, ev) -> None:
        """Guarda los parpadeos del jugador: son los que se dibujan en el ojo de la imagen sintética."""
        if ev.actor == PLAYER and ev.player_blink is not None:
            self.game.blinks.append(GroundTruthBlink(ev.time_s, ev.player_blink, ev.advance))

    # --- «Capturadora» ----------------------------------------------------------------------------
    def read(self) -> Frame:
        """Siguiente frame (avanza el reloj simulado 1/fps). Muestra el juego con retraso de vídeo."""
        t = self.clock.now()
        self.clock.advance(1.0 / self.scene.fps)
        t_game = t - self.truth.video_latency_s
        self._advance_game(t_game)
        g = self.game
        if g.location == "door" and t_game >= g.door_until:
            g.location = "cave"
        if g.phase == "idle" and g.location != "start" and g.menu == MENU_CLOSED:
            image = self._cave_screen(g.location)
        elif g.phase == "timeline" and self.truth.message_scene:
            image = self._draw_dialog(self._cave_screen("cave"))  # diálogo con el legendario
        elif g.phase == "idle" and g.menu not in (MENU_CLOSED, MENU_OPEN):
            image = self._menu_screen(g.menu)
        elif g.phase == "idle":
            image = self.renderer.render(openness_at(t_game, g.blinks[-3:], self.scene.close_s, self.scene.double_gap_s))
            if g.menu == MENU_OPEN:
                image = self._draw_menu(image.copy(), g.cursor)
            elif g.message:
                image = self._draw_dialog(image.copy())
        elif g.phase == "timeline":
            image = self.renderer.render(1.0)
        else:
            image = np.full((self.scene.height, self.scene.width, 3), (40, 40, 200), np.uint8)  # "combate"
        self.frame_index += 1
        return Frame(image, t, self.frame_index)

    def _cave_screen(self, location: str) -> np.ndarray:
        """Pantalla de la ruta hasta el legendario: negro durante la puerta y un bloque que lo representa."""
        h, w = self.scene.height, self.scene.width
        if location == "door":
            return np.zeros((h, w, 3), np.uint8)  # fundido a negro
        img = np.full((h, w, 3), (60, 70, 80), np.uint8)
        img[int(0.3 * h):int(0.5 * h), int(0.45 * w):int(0.55 * w)] = (200, 180, 90)  # el legendario
        return img

    def _draw_dialog(self, img: np.ndarray) -> np.ndarray:
        """Cuadro de diálogo como el de BDSP: interior blanco y marco con detalle en los extremos."""
        h, w = img.shape[:2]
        y0, y1 = int(0.82 * h), int(0.97 * h)
        img[y0:y1, int(0.17 * w):int(0.84 * w)] = (250, 250, 250)
        for x0, x1 in ((0.17, 0.20), (0.81, 0.84)):  # marco con rayas (variación de color)
            for k, y in enumerate(range(y0, y1, 3)):
                img[y:y + 2, int(x0 * w):int(x1 * w)] = (90, 90, 200) if k % 2 else (230, 140, 160)
        return img

    def _draw_menu(self, img: np.ndarray, cursor: int) -> np.ndarray:
        """Menú como el de BDSP: panel blanco con 2×4 iconos de colores (franjas blancas entre columnas)."""
        h, w = img.shape[:2]
        img[int(0.08 * h):int(0.66 * h), int(0.12 * w):int(0.88 * w)] = (250, 250, 250)
        colors = [(40, 120, 230), (90, 90, 230), (40, 200, 230), (200, 90, 220),
                  (60, 180, 60), (220, 110, 60), (200, 80, 120), (230, 80, 200)]
        for k, color in enumerate(colors):
            row, col = divmod(k, 4)
            x0 = int((0.183 + 0.166 * col) * w)
            y0 = int((0.14 + 0.24 * row) * h)
            img[y0:y0 + int(0.18 * h), x0:x0 + int(0.12 * w)] = color
            if k == cursor:  # resalte del icono seleccionado
                img[y0:y0 + 3, x0:x0 + int(0.12 * w)] = (255, 255, 255)
        return img

    def _menu_screen(self, menu: int) -> np.ndarray:
        """Pantallas de menú simplificadas: cada una con colores propios para que se distingan entre sí."""
        h, w = self.scene.height, self.scene.width
        img = np.zeros((h, w, 3), np.uint8)
        if menu == POKEDEX:  # lista: fondo rojo con filas claras
            img[:] = (40, 40, 190)
            for y in range(10, h - 10, 22):
                img[y:y + 14, w // 2:w - 10] = (230, 230, 230)
        elif menu == POKEDEX_ENTRY:  # ficha: azul con un recuadro
            img[:] = (160, 90, 40)
            img[20:h - 20, 20:w // 2] = (240, 240, 240)
        elif menu == TOWN_MAP:  # mapa: verde con «rutas»
            img[:] = (90, 150, 80)
            img[h // 2 - 4:h // 2 + 4, :] = (200, 220, 230)
            img[:, w // 3 - 4:w // 3 + 4] = (200, 220, 230)
        else:  # otra pantalla (p. ej. la mochila)
            img[:] = (60, 170, 60)
            img[h // 3:2 * h // 3, :] = (20, 90, 20)
        return img

    def info(self) -> SourceInfo:
        return SourceInfo("switch-simulada", self.scene.width, self.scene.height, self.scene.fps, "SIM")

    def close(self) -> None:
        """No hay nada que liberar; existe para cumplir la interfaz de las fuentes de frames."""

    def __iter__(self) -> Iterator[Frame]:
        while True:
            yield self.read()

    # --- Detectores/observación «oráculo» (solo simulación) -------------------------------------
    def in_battle(self, frame: Frame) -> bool:
        """El combate simulado es una pantalla roja uniforme (BGR): basta con mirar un píxel."""
        return bool(frame.image[0, 0, 2] > 150 and frame.image[0, 0, 0] < 80)

    def observe(self, fields: list[str]) -> Observation | None:
        """Lo que la verificación leería de la pantalla de datos."""
        if self.game.generated is None:
            return None
        return observe_like(self.game.generated, fields)

    # --- Mando ------------------------------------------------------------------------------------
    def controller(self) -> "SimController":
        """Mando que actúa sobre esta Switch simulada."""
        return SimController(self)


class SimController(Controller):
    """Mando que entrega las pulsaciones al juego simulado con la latencia de entrada de ``SimTruth``.

    Como el dispositivo real, encadena los comandos sin huecos tras lo ya encolado.
    """

    def __init__(self, switch: SimulatedSwitch) -> None:
        self.switch = switch
        self.log: list[tuple[float, Command]] = []
        self._connected = False
        self._busy_until = 0.0

    def connect(self) -> ControllerStatus:
        self._connected = True
        return self.status()

    def close(self) -> None:
        self._connected = False

    def status(self) -> ControllerStatus:
        return ControllerStatus(self._connected, self._connected, "sim", "sim")

    def submit(self, commands: list[Command]) -> None:
        t = max(self.switch.clock.now(), self._busy_until)
        for cmd in commands:
            self.log.append((t, cmd))
            if cmd.state.buttons:
                self.switch.inputs.append((t + self.switch.truth.input_latency_s, cmd.state.buttons))
            if cmd.state.dpad is not DPad.NONE:
                self.switch.holds.append((t + self.switch.truth.input_latency_s, cmd.state.dpad, cmd.duration_ms / 1000))
            # La inclinación de «keep-awake» solo evita que la consola se suspenda: no es una entrada de juego.
            if cmd.state.right != (0.0, 0.0) and cmd.label != "keep-awake":
                self.switch.sticks.append((t + self.switch.truth.input_latency_s, cmd.state.right))
            t += cmd.duration_ms / 1000
        self._busy_until = t

    def wait_idle(self, timeout_s: float | None = None) -> None:
        self.switch.clock.sleep_until(self._busy_until)

    def emergency_stop(self) -> None:
        # Descarta todo lo que aún no ha llegado al juego, como el CQ_CANCEL del ESP32.
        self.switch.inputs.clear()
        self.switch.holds.clear()
        self.switch.sticks.clear()
        self._busy_until = self.switch.clock.now()
