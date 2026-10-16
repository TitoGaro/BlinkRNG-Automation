"""Base de la ejecución: vídeo, ojo, mensaje de la escena, pulsaciones y comprobación de pantallas.

``RunBase`` reúne lo que usan todas las fases. Las fases son *mixins* (``SeedMixin``, ``RouteMixin``,
``FrontMixin``, ``CaptureMixin``) que se combinan en ``StaticEncounterRun`` (``static_strategy.py``)."""
from __future__ import annotations

import datetime as dt
import itertools
import math
import time
from collections.abc import Callable
from dataclasses import replace

from rngauto.control.controller import Button, Command, ControllerState, DPad, NEUTRAL
from rngauto.core.calibration import AttemptRecord
from rngauto.core.statemachine import InterventionRequired, StateContext
from rngauto.i18n import t
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.frames import Frame
from rngauto.vision.screen import changed_fraction, similarity, thumbnail

from ..blink_pipeline import BlinkRecovery
from ..screens import BUILTIN_SCREENS, dialog_visible, menu_visible
from .config import Mode, RunStatus, StaticDeps, StaticRunConfig, screen_name


SCENE_RESTORED_FRACTION = 0.15  # píxeles cambiados respecto a la escena normal para darla por recuperada
SCREEN_MATCH = 0.5  # parecido mínimo con la firma (Pokédex real: 0,60-1,00 según la ficha; otras pantallas ≤ 0,01)


class RunBase:
    """Estado común de la ejecución y utilidades de escena que usan todas las fases."""

    def __init__(self, deps: StaticDeps, cfg: StaticRunConfig) -> None:
        self.d = deps
        self.cfg = cfg
        self.attempt_no = 0
        self._battle_possible = False  # se pulsó la A final (o empezó el combate): nada de reiniciar solo
        self._auto_restarts = 0
        self._early_starts = 0  # intentos seguidos con el combate empezado antes de la A final
        self._extra_seen: list[int] = []  # adelanto medido del contador en la ruta (legendario a la vista)
        self.attempts: list[AttemptRecord] = []
        self.last_recovery: BlinkRecovery | None = None
        self._ids = itertools.count(1)
        self.status = RunStatus(delay_desc=deps.calibration.estimate_final_delay().describe(),
                                blinks_needed=deps.encounter.blink_scene.min_blinks)
        self._next_nudge = -math.inf
        self._next_warning_check = 0.0
        self._fast_count = 0
        self._fast_disabled: set[str] = set()
        self._fast_failures: dict[str, int] = {}  # fallos por acción: tras un reinicio se vuelve a probar
        self._fast_declined = False  # modo asistido: el usuario rechazó la Pokédex (no se vuelve a ofrecer)
        self._fast_approved: bool | None = None  # modo asistido: el usuario autoriza la Pokédex una vez
        self._advance_offset = 0
        self.closers: list[Callable[[], None]] = []  # p. ej. soltar el anillo de sonido al terminar
        self._wall_offset = time.time() - deps.clock.now()  # reloj de la estrategia → hora del reloj de pared

    def _fmt_t(self, t: float) -> str:
        """Instante del reloj de la estrategia como hora legible («20:41:03.25»)."""
        return dt.datetime.fromtimestamp(t + self._wall_offset).strftime("%H:%M:%S.%f")[:-4]

    def close(self) -> None:
        """Libera los recursos registrados en ``closers``; un fallo de uno no impide cerrar los demás."""
        for fn in self.closers:
            try:
                fn()
            except Exception:
                pass
        self.closers.clear()

    def _emit(self, **changes) -> None:
        """Actualiza ``status`` y lo envía a la interfaz. Un error de la interfaz nunca detiene la estrategia."""
        self.status = replace(self.status, **changes)
        if self.d.on_status is not None:
            try:
                self.d.on_status(self.status)
            except Exception:
                pass

    def _check_controller(self, what: str) -> None:
        """Para con ``InterventionRequired`` no recuperable si el mando no está emparejado con la consola.

        ``what`` describe el momento (aparece en el mensaje). En modo ensayo no se comprueba.
        """
        if self.cfg.mode is Mode.DRY_RUN:
            return
        status = self.d.controller.status()
        if not (status.connected and status.console_ready) and hasattr(self.d.controller, "refresh_status"):
            try:  # el estado en caché puede estar algo viejo: se pregunta al ESP32 antes de abortar
                status = self.d.controller.refresh_status()
            except Exception:
                pass
        if not (status.connected and status.console_ready):
            raise InterventionRequired(t("El mando no está conectado a la Switch ({what}): {detail}",
                                         "The controller is not connected to the Switch ({what}): {detail}",
                                         what=what, detail=status.detail),
                                       recoverable=False)

    def _press(self, ctx: StateContext, button: Button, label: str) -> None:
        # Nunca se pide confirmación aquí: la pulsación debe salir en su instante exacto.
        # En modo asistido el usuario aprueba el plan completo antes (estado PLAN).
        self.d.controller.submit([Command(ControllerState(button), 80, label), Command(NEUTRAL, 80, label)])
        ctx.log("input", f"{button.name}: {label}")

    def _report_frame_overruns(self, ctx: StateContext) -> None:
        """Avisa si la capturadora tuvo que descartar frames porque el programa se retrasó al leerlos (CPU
        ocupada, antivirus…): un parpadeo en ese tramo se pierde, y así queda constancia en el registro."""
        source = self.d.frames
        stats = getattr(source, "stats", None) or getattr(getattr(source, "frames", None), "stats", None)
        overruns = getattr(stats, "overruns", 0)
        seen = getattr(self, "_overruns_seen", 0)
        if isinstance(overruns, int) and overruns > seen:
            ctx.log("warning", t("Se han perdido {n} frames: el programa se retrasó al leer la imagen (¿el PC va "
                                 "muy cargado?)", "{n} frames were lost: the program fell behind reading the video "
                                 "(is the PC very busy?)", n=overruns - seen))
            self._overruns_seen = overruns

    def _send_final(self, ctx: StateContext, press) -> str:
        """Pulsación final, la que genera el Pokémon. Desde aquí puede haber un shiny en pantalla, así que el
        juego ya no se reinicia solo; y el intento ha llegado hasta el final (se ponen a cero los reinicios por
        imprevistos)."""
        self._battle_possible = True
        self._press(ctx, Button[press.trigger.upper()], "final")
        ctx.data["final_sent_t"] = press.send_t
        self._auto_restarts = 0
        return "ENCOUNTER"

    def _keep_awake(self, ctx: StateContext, next_press_t: float | None = None) -> None:
        """Toque de stick derecho cada ``keep_awake_s`` para que la Switch no atenúe ni suspenda la pantalla.
        Nunca en modo ensayo ni cerca de una pulsación planificada."""
        if self.cfg.mode is Mode.DRY_RUN:
            return
        now = ctx.now()
        if self._next_nudge == -math.inf:
            self._next_nudge = now + self.cfg.keep_awake_s
        if now < self._next_nudge:
            return
        if next_press_t is not None and abs(next_press_t - now) < self.cfg.keep_awake_guard_s:
            return
        self.d.controller.submit([Command(ControllerState(right=(1.0, 0.0)), 80, "keep-awake"),
                                  Command(NEUTRAL, 80, "keep-awake")])
        self._next_nudge = now + self.cfg.keep_awake_s

    def _next_frame(self, ctx: StateContext) -> Frame:
        """Siguiente frame; tolera cortes breves y para de forma segura si la imagen no vuelve."""
        if time.monotonic() >= self._next_warning_check:  # incidencias del mando, al registro (cada ~2 s)
            self._next_warning_check = time.monotonic() + 2.0
            for warning in self.d.controller.take_warnings():
                ctx.log("warning", warning)
            self._report_frame_overruns(ctx)
        gap_started: float | None = None
        while True:
            frame = self.d.frames.read()
            ctx.check()
            if frame is not None:
                return frame
            if gap_started is None:
                gap_started = time.monotonic()
                ctx.log("warning", t("No llegan frames de la capturadora; esperando…",
                                     "No frames arriving from the capture card; waiting…"))
            elif time.monotonic() - gap_started > self.cfg.frame_gap_timeout_s:
                raise InterventionRequired(t("Se ha perdido la imagen de la capturadora",
                                             "Lost the video from the capture card"), recoverable=False)

    def _wait_for_scene(self, ctx: StateContext) -> None:
        """Antes de contar parpadeos, el ojo debe verse de forma estable (descarta frames viejos de cargas,
        menús o transiciones): 10 frames seguidos con correlación ≥ ``scene_found_score``."""
        self._emit(message=t("Buscando el ojo de tu personaje en la imagen…",
                             "Looking for your character's eye in the video…"))
        deadline = ctx.now() + self.cfg.scene_timeout_s
        streak = 0
        best = 0.0
        while ctx.now() < deadline:
            score, valid = self.d.matcher.score(self._next_frame(ctx).image)
            best = max(best, score if valid else 0.0)
            streak = streak + 1 if valid and score >= self.cfg.scene_found_score else 0
            if streak >= 10:
                ctx.log("detection", t("Ojo localizado (correlación {score:.2f})",
                                       "Eye found (correlation {score:.2f})", score=score))
                return
        raise InterventionRequired(t(
            "No encuentro el ojo de tu personaje (mejor correlación {best:.2f}). ¿Estás en la escena correcta y "
            "quieto? Si cambiaste de sitio o de zoom, vuelve a marcar el ojo (paso «Escena»).",
            "Cannot find your character's eye (best correlation {best:.2f}). Are you in the right scene and "
            'standing still? If you moved or changed the zoom, mark the eye again ("Scene" step).', best=best))

    def _wait_until(self, ctx: StateContext, until: float) -> None:
        """Como ``ctx.sleep_until``, pero leyendo vídeo y manteniendo despierta la consola: delante del
        legendario la espera hasta su momento puede ser de varios minutos."""
        while ctx.now() < until - 0.1:
            self._next_frame(ctx)
            self._keep_awake(ctx, next_press_t=until)
        ctx.sleep_until(until)

    def _pass_time(self, ctx: StateContext, seconds: float) -> Frame | None:
        """Deja pasar ``seconds`` leyendo frames (el búfer no se llena y la PARADA se atiende)."""
        end = ctx.now() + seconds
        last = None
        while ctx.now() < end:
            last = self._next_frame(ctx)
        return last

    def _eye_visible(self, frame: Frame, matcher: EyeMatcher | None = None) -> bool:
        score, valid = (matcher or self.d.matcher).score(frame.image)
        return valid and score >= self.cfg.scene_found_score

    def _scene_reference(self, ctx: StateContext, matcher: EyeMatcher | None = None) -> object:
        """Miniatura de la escena normal (ojo visible y estable) antes de tocar ningún menú. ``matcher``: otro
        ojo (delante del legendario); por defecto, el de la escena de parpadeos."""
        streak, last = 0, None
        deadline = ctx.now() + 10.0
        while ctx.now() < deadline:
            frame = self._next_frame(ctx)
            streak = streak + 1 if self._eye_visible(frame, matcher) else 0
            last = frame
            if streak >= 10:
                return thumbnail(last)
        raise InterventionRequired(t("No veo el ojo de tu personaje: no abro ningún menú sin ver la escena",
                                     "Cannot see your character's eye: no menu is opened without seeing the scene"))

    def _scene_normal(self, ctx: StateContext, reference, seconds: float, matcher: EyeMatcher | None = None) -> bool:
        """¿Vuelve a verse la escena de ``reference`` (ojo visible, poca diferencia y, si la escena lleva el
        mensaje de Serbal, con el mensaje) durante 5 frames seguidos antes de ``seconds``?"""
        streak = 0
        end = ctx.now() + seconds
        while ctx.now() < end:
            frame = self._next_frame(ctx)
            ok = (self._eye_visible(frame, matcher)
                  and changed_fraction(thumbnail(frame), reference) < SCENE_RESTORED_FRACTION)
            if ok and self._message_scene and not dialog_visible(frame.image):
                ok = False
            streak = streak + 1 if ok else 0
            if streak >= 5:
                return True
        return False

    @property
    def _message_scene(self) -> bool:
        return self.d.encounter.blink_scene.message

    def _use_registered_item(self, ctx: StateContext) -> None:
        """«+» abre la rueda de objetos registrados y la dirección elige uno con la cruceta (la rueda muestra «L»
        con flechas). Así se usa la caña y aparece el mensaje del profesor Serbal."""
        label = t("objeto registrado (caña)", "registered item (rod)")
        direction = self.cfg.registered_item_direction.upper()
        cmds = [Command(ControllerState(Button.PLUS), 100, label), Command(NEUTRAL, 900, label)]
        if direction != "NONE":
            cmds += [Command(ControllerState(dpad=DPad[direction]), 120, label), Command(NEUTRAL, 300, label)]
        self.d.controller.submit(cmds)
        ctx.log("input", t("Objeto registrado: «+» {direction}", 'Registered item: "+" {direction}',
                           direction=direction if direction != "NONE" else "").strip())
        self._pass_time(ctx, 2.5)

    def _dismiss_message(self, ctx: StateContext) -> bool:
        """Quita el mensaje de la escena con A y comprueba que ya no está."""
        for _ in range(2):
            self._press(ctx, Button.A, t("quitar mensaje", "dismiss message"))
            if self._wait_screen(ctx, lambda f: not dialog_visible(f.image), 2.5, streak_needed=5):
                return True
        ctx.log("warning", t("El mensaje no se ha quitado", "The message was not dismissed"))
        return False

    def _show_message(self, ctx: StateContext) -> bool:
        """Usa la caña para que aparezca el mensaje (si no está ya)."""
        if self._wait_screen(ctx, lambda f: dialog_visible(f.image), 0.5):
            return True
        self._use_registered_item(ctx)
        ok = self._wait_screen(ctx, lambda f: dialog_visible(f.image), 3.0)
        ctx.log("detection", t("Mensaje en pantalla", "Message on screen") if ok else
                t("No aparece el mensaje tras usar el objeto registrado",
                  "The message did not appear after using the registered item"))
        return ok

    def _restore_scene(self, ctx: StateContext, reference, attempts: int = 6,
                       matcher: EyeMatcher | None = None) -> bool:
        """Vuelve a la escena normal: B para cerrar menús y pantallas y, si la escena se observa con el mensaje
        de Serbal y falta, usar la caña. Se comprueba cada vez con la imagen."""
        item_uses = 0
        for _ in range(attempts):
            if self._scene_normal(ctx, reference, 2.0, matcher):
                return True
            # Varios frames y hasta 3 s: al cerrar el menú el personaje hace un gesto (~2 s) que tapa el ojo; con
            # un solo frame se confundiría con un menú abierto y se pulsaría B en bucle.
            overworld = self._wait_screen(ctx, lambda f: (self._eye_visible(f, matcher) and not menu_visible(f.image)
                                                          and not dialog_visible(f.image)), 3.0)
            if self._message_scene and overworld and item_uses < 2:
                item_uses += 1
                self._show_message(ctx)
                continue
            self._press(ctx, Button.B, t("cerrar menú", "close menu"))
            self._pass_time(ctx, 1.2)
        return self._scene_normal(ctx, reference, 3.0, matcher)

    def _fast_failed(self, method_id: str) -> None:
        """La acción de Pokédex no fue como se esperaba: no se usa más en este intento (y nunca más tras
        ``FAST_FAIL_MAX`` fallos). Tras reiniciar el juego se vuelve a probar: casi siempre es un fallo puntual."""
        self._fast_disabled.add(method_id)
        self._fast_failures[method_id] = self._fast_failures.get(method_id, 0) + 1

    def _wait_screen(self, ctx: StateContext, test: Callable[[Frame], bool], timeout_s: float,
                     streak_needed: int = 3) -> bool:
        """Espera (como mucho ``timeout_s``) a que ``test`` se cumpla en varios frames seguidos: una carga
        algo más lenta de lo previsto no da un falso fallo."""
        end = ctx.now() + timeout_s
        streak = 0
        while ctx.now() < end:
            streak = streak + 1 if test(self._next_frame(ctx)) else 0
            if streak >= streak_needed:
                return True
        return False

    def _check_screen(self, ctx: StateContext, name: str, timeout_s: float = 3.0) -> bool:
        """¿Se ve la pantalla ``name``? Las conocidas (el menú) se reconocen siempre; las demás (la Pokédex)
        se aprenden la primera vez con confirmación del usuario y después se reconocen solas. Con prefijo
        ``!`` se comprueba lo contrario: que esa pantalla conocida NO esté."""
        if name.startswith("!"):  # que NO se vea esa pantalla (p. ej. ningún diálogo abierto)
            base = BUILTIN_SCREENS.get(name[1:])
            if base is None:
                raise ValueError(t("Comprobación negada solo para pantallas conocidas: {name}",
                                   "Negated check only for known screens: {name}", name=name))
            ok = self._wait_screen(ctx, lambda f: not base(f.image), timeout_s, streak_needed=5)
            ctx.log("detection", t("Sin «{screen}» en pantalla: {ok}", 'No "{screen}" on screen: {ok}',
                                   screen=screen_name(name[1:]), ok=t("sí", "yes") if ok else "NO"))
            return ok
        label = screen_name(name)
        detector = BUILTIN_SCREENS.get(name)
        if detector is not None:
            ok = self._wait_screen(ctx, lambda f: detector(f.image), timeout_s)
            ctx.log("detection", t("Pantalla «{label}»: {result}", 'Screen "{label}": {result}', label=label,
                                   result=t("reconocida", "recognized") if ok else t("NO reconocida", "NOT recognized")))
            return ok
        signature = self.d.screens.get(name)
        if signature is not None:
            best = [0.0]

            def matches(f: Frame) -> bool:
                score = similarity(thumbnail(f), signature)
                best[0] = max(best[0], score)
                return score >= SCREEN_MATCH

            ok = self._wait_screen(ctx, matches, timeout_s)
            ctx.log("detection", t("Pantalla «{label}»: parecido {score:.2f} (mínimo {minimum})",
                                   'Screen "{label}": similarity {score:.2f} (minimum {minimum})',
                                   label=label, score=best[0], minimum=SCREEN_MATCH))
            return ok
        self._pass_time(ctx, 1.0)  # que termine de cargar antes de preguntar
        ok = self.d.operator.confirm(t(
            "Primera vez con el avance rápido: ¿se ve ahora la {label} en la imagen del juego?\n\n"
            "Si respondes «Sí», el programa recordará esta pantalla y la reconocerá solo a partir de ahora.",
            "First time using fast advance: is the {label} showing in the game video right now?\n\n"
            'If you answer "Yes", the program will remember this screen and recognize it on its own from now on.',
            label=label),
            timeout_s=90.0)
        ctx.check()
        if ok:
            self.d.screens.put(name, thumbnail(self._next_frame(ctx)))
            ctx.log("detection", t("Pantalla «{label}» aprendida", 'Screen "{label}" learned', label=label))
            return True
        return False
