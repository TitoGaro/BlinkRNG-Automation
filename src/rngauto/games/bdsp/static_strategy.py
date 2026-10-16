"""Estrategia genérica de encuentro estático BDSP (la ejecuta cualquier ``EncounterConfiguration``).

Estados: PRECHECK → COLLECT (parpadeos → estado, con confirmación) → PLAN (objetivo + plan con márgenes)
→ [FAST_ADVANCE (Pokédex) → REIDENTIFY (~7 parpadeos) → PLAN, mientras falte mucho] → WAIT_TRIGGER (vigila
la sincronía, pulsa en el centro del periodo) → TIMELINE (pulsación final en el centro del hueco) →
ENCOUNTER (detecta el combate y si es shiny: si lo es, PARA y el usuario lo captura) → CATCH (Master Ball,
textos, ficha) → VERIFY (lee la ficha, identifica, calibra) → RESET (cierra el juego desde HOME) → COLLECT.

No sabe de hardware: recibe un reloj, una fuente de frames, un mando, detectores y un operador. Corre
igual contra la Switch simulada (tests, DRY RUN acelerado) y contra la real.

El código de cada fase está en ``static_run/``; aquí se combinan (``StaticEncounterRun``) y se
reexportan los nombres públicos.
"""
from __future__ import annotations

from rngauto.control.controller import Button, Command, ControllerError, ControllerState, NEUTRAL
from rngauto.core.statemachine import InterventionRequired, StateContext, StateMachine, StateSpec
from rngauto.i18n import t

from .static_run.config import (
    AutoOperator, FAST_FAIL_MAX, Mode, Operator, RunStatus, StaticDeps, StaticRunConfig, fmt_duration,
)
from .static_run.base import RunBase
from .static_run.seed import SeedMixin
from .static_run.route import RouteMixin
from .static_run.front import FrontMixin
from .static_run.capture import CaptureMixin


__all__ = ["AutoOperator", "Mode", "Operator", "RunStatus", "StaticDeps", "StaticEncounterRun", "StaticRunConfig",
    "fmt_duration"]


class StaticEncounterRun(SeedMixin, RouteMixin, FrontMixin, CaptureMixin, RunBase):
    """Una ejecución completa de la estrategia: cada método público es un estado de la máquina.

Los estados devuelven el nombre del siguiente. Todo lo que toca el juego pasa por el mando de ``deps`` y se
comprueba con la imagen; ante algo inesperado se pide intervención en vez de seguir pulsando a ciegas."""

    def precheck(self, ctx: StateContext) -> str:
        """PRECHECK: encuentro soportado, mando emparejado y vídeo llegando. Si algo falla, para sin pulsar."""
        self._emit(phase="PRECHECK", message=t("Comprobando mando y vídeo", "Checking controller and video"))
        self.d.controller.resume()  # una PARADA anterior bloquea el mando hasta que empieza otra ejecución
        enc = self.d.encounter
        if enc.blink_scene.scene.pokemon_npc:
            raise InterventionRequired(t("Este encuentro observa parpadeos con un Pokémon en escena: aún no está soportado",
                                         "This encounter observes blinks with a Pokémon in the scene: not supported yet"),
                                       recoverable=False)
        if len(enc.timeline) > 1:
            raise InterventionRequired(t("Este encuentro tiene un timeline de varias fases: aún no está soportado",
                                         "This encounter has a multi-phase timeline: not supported yet"),
                                       recoverable=False)
        status = self.d.controller.status()
        if not status.connected:
            raise InterventionRequired(t("Mando no conectado: {detail}", "Controller not connected: {detail}",
                                         detail=status.detail), recoverable=False)
        self._check_controller(t("antes de empezar", "before starting"))
        self._next_frame(ctx)
        ctx.log("state", t("Hardware y vídeo OK", "Hardware and video OK"))
        return "COLLECT"

    @property
    def _can_reset_from_home(self) -> bool:
        return self.d.reset_game is None and self.cfg.reset_from_home and self.cfg.mode is not Mode.DRY_RUN

    def _reset_from_home(self, ctx: StateContext) -> None:
        """Cierra el juego desde HOME (sin guardar) y lo vuelve a abrir pulsando A hasta ver el ojo.

        Sin reconocer pantallas (vale para cualquier consola, idioma o tema): HOME → X («¿Quieres cerrar el
        programa?», con el cursor en «Cerrar») → A → A repetida (abre el juego, elige el perfil, salta logos y
        título) hasta ver el ojo que marcó el usuario. La secuencia sirve esté como esté: con el juego abierto lo
        cierra; si ya estaba cerrado, X no hace nada y A lo abre. Nunca se pulsa R (guardar). Medido en consola
        (Switch 2): la partida vuelve en ~30 s. La espera tras HOME es generosa porque, si X llega antes de que el
        menú responda, no sale la pregunta. Se intenta dos veces; si no vuelve el ojo, se pide intervención."""
        label = t("reiniciar el juego", "restart the game")
        # Lo que quedara encolado (p. ej. la cruceta de la Pokédex) se ejecutaría antes que HOME y se comería
        # las esperas: HOME, X y A saldrían seguidas y X llegaría antes de que el menú responda.
        if not self.d.controller.clear_queue(timeout_s=1.0):
            ctx.log("warning", t("No consta que la cola del mando se vaciara antes de reiniciar",
                                 "No confirmation that the controller queue was emptied before restarting"))
        for attempt in range(2):
            self._press(ctx, Button.HOME, label)
            self._pass_time(ctx, 3.0)
            self._press(ctx, Button.X, label)
            self._pass_time(ctx, 1.5)
            self._press(ctx, Button.A, label)  # «Cerrar»
            self._pass_time(ctx, 3.5)
            end = ctx.now() + self.cfg.reset_timeout_s
            presses = 0
            while ctx.now() < end:
                self.d.controller.submit([Command(ControllerState(Button.A), 80, label), Command(NEUTRAL, 80, label)])
                presses += 1
                if self._wait_screen(ctx, self._eye_visible, 0.6, streak_needed=10):
                    ctx.log("state", t("Juego reiniciado ({presses} A hasta volver a ver el ojo)",
                                       "Game restarted ({presses} A presses until the eye was visible again)",
                                       presses=presses))
                    self._pass_time(ctx, 1.0)
                    return
            ctx.log("warning", t("Tras reiniciar no veo el ojo de tu personaje: se repite el reinicio",
                                 "Cannot see your character's eye after restarting: restarting again"))
        raise InterventionRequired(t("Tras reiniciar el juego no vuelvo a ver la escena del ojo (¿se guardó la partida "
                                     "en el punto de inicio, mirando a la cámara?)",
                                     "After restarting the game, the eye scene does not come back (was the game "
                                     "saved at the starting point, facing the camera?)"))

    def reset(self, ctx: StateContext) -> str:
        """RESET: reinicia el juego para otro intento (desde HOME, con la función de ``deps`` o pidiéndolo al
        usuario) y vuelve a COLLECT. Para al agotar ``max_attempts``."""
        self.attempt_no += 1
        if self.attempt_no >= self.cfg.max_attempts:
            raise InterventionRequired(t("Agotados {n} intentos sin éxito", "{n} attempts used up without success",
                                         n=self.cfg.max_attempts), recoverable=False)
        ctx.log("state", t("Reintento {n}: reiniciando el juego", "Retry {n}: restarting the game", n=self.attempt_no))
        self._battle_possible = False
        if not self._fast_declined:  # un fallo de la Pokédex en el intento anterior no la descarta para siempre
            self._fast_disabled -= {i for i, n in self._fast_failures.items() if n < FAST_FAIL_MAX}
        self._emit(phase="RESET", attempt=self.attempt_no + 1,
                   message=t("Reiniciando el juego para otro intento", "Restarting the game for another attempt"))
        if self._can_reset_from_home:
            self._reset_from_home(ctx)
        elif self.d.reset_game is None:
            ready = self.d.operator.wait_ready(
                t("Reinicia el juego, carga la partida y vuelve a la escena de parpadeos sin moverte",
                  "Restart the game, load the save and go back to the blink scene without moving")
                + (t(" (el mensaje del profesor Serbal lo pondrá el programa con la caña)",
                     " (the program will bring up Professor Rowan's message with the rod)")
                   if self._message_scene else ""))
            ctx.check()
            if not ready:
                raise InterventionRequired(t("No se confirmó que el juego esté listo", "The game was not confirmed as ready"),
                                           recoverable=False)
        else:
            self.d.reset_game()
        return "COLLECT"

    def build(self) -> StateMachine:
        """Máquina de estados con los tiempos máximos y reintentos de cada estado. Una PARADA activa la parada
        de emergencia del mando; los fallos pasan por ``_on_failure``."""
        c = self.cfg
        states = [
            StateSpec("PRECHECK", self.precheck, timeout_s=30, max_retries=3),
            StateSpec("COLLECT", self.collect, timeout_s=c.collect_timeout_s, max_retries=2),
            StateSpec("PLAN", self.plan),  # puede esperar la aprobación del usuario
            StateSpec("FAST_ADVANCE", self.fast_advance, max_retries=0),  # acotado por dentro
            StateSpec("REIDENTIFY", self.reidentify, max_retries=0),  # acotado por dentro (→ COLLECT)
            StateSpec("WAIT_TRIGGER", self.wait_trigger, max_retries=0),
            StateSpec("TIMELINE", self.timeline, timeout_s=3600),
            # Si lo captura el usuario (Cresselia, errante) puede tardar lo que quiera: sin tiempo máximo.
            StateSpec("ENCOUNTER", self.encounter,
                      timeout_s=(None if self.d.encounter.capture == "user"
                                 or (self.d.in_battle is None and not c.auto_battle) else c.battle_timeout_s + 30),
                      max_retries=0),
            # Peor caso: Master Ball con búsqueda en la Bolsa (~70 s) + captura (120 s) + ficha.
            StateSpec("CATCH", self.catch, timeout_s=360, max_retries=0),
            StateSpec("VERIFY", self.verify),  # el usuario puede tardar en capturar y leer los datos
            StateSpec("RESET", self.reset, max_retries=1),
        ]
        return StateMachine(states, "PRECHECK", self.d.clock, self.d.timeline,
                            on_stop=[self.d.controller.emergency_stop], on_failure=self._on_failure)

    def _on_failure(self, state: str, exc: Exception) -> str | None:
        """Ante algo imprevisto ANTES de la A final (no encuentra el ojo, no sale el mensaje, la ruta se tuerce, un
        error…) se cierra el juego y se empieza de nuevo en vez de parar: no hay nada que perder. Igual para todos
        los encuentros. Nunca después de la A final (podría haber un shiny en pantalla), ni si reiniciar no lo
        arregla (sin mando, sin imagen…), ni más de ``max_auto_restarts`` veces seguidas sin completar un intento."""
        if isinstance(exc, InterventionRequired) and not exc.recoverable:
            return None
        if isinstance(exc, ControllerError):
            return None  # sin mando, reiniciar desde HOME tampoco funcionaría
        if self._battle_possible or state in ("ENCOUNTER", "CATCH", "VERIFY") or not self._can_reset_from_home:
            return None
        if self._auto_restarts >= self.cfg.max_auto_restarts:
            self.d.timeline.add(self.d.clock.now(), "error",
                                t("Ya se ha reiniciado solo {n} veces seguidas sin completar un "
                                  "intento: se para para que lo revises",
                                  "It has already restarted on its own {n} times in a row without completing an "
                                  "attempt: stopping so you can check it", n=self._auto_restarts))
            return None
        self._auto_restarts += 1
        self.d.timeline.add(self.d.clock.now(), "warning",
                            t("Imprevisto en {state}: {exc}. Se cierra el juego y se empieza de nuevo "
                              "({n}/{max})",
                              "Unexpected problem in {state}: {exc}. Closing the game and starting again "
                              "({n}/{max})", state=state, exc=exc, n=self._auto_restarts,
                              max=self.cfg.max_auto_restarts))
        return "RESET"
