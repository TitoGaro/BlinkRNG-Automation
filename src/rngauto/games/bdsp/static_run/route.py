"""Ruta hasta el legendario y timeline: pasos de macro (diálogos, andar y hablar), aproximación antes de la
pulsación y TIMELINE (pulsación final en el centro del hueco)."""
from __future__ import annotations

import math

from rngauto.control.controller import Button, Command, ControllerState, DPad, NEUTRAL, split_long
from rngauto.core.statemachine import InterventionRequired, StateContext
from rngauto.i18n import t
from rngauto.vision.frames import Frame
from rngauto.vision.screen import changed_fraction, thumbnail

from ..battle import TEXT_REGION, choice_box_visible
from ..fast_advance import AdvanceMethod, DIALOG_KINDS, MacroStep
from ..planner import Plan
from ..screens import battle_menu_visible, dialog_arrow_visible, dialog_visible
from ..shiny import battle_dialog_visible
from .config import POKEMON_MEAN_INTERVAL_S, screen_name


# Medido en consola: ~60 ms de cruceta solo giran al personaje; ~250 ms avanzan una casilla.
WALK_TALK_WAIT_S = 1.5  # tras cada A, cuánto vídeo se mira buscando su texto antes de seguir
BUMP_MS = 300  # empujar contra algo que bloquea: solo gira al personaje
FRONT_EXTRA_MARGIN = 1500  # margen sobre el mayor desvío visto al llegar al legendario (con avance rápido allí)
WALK_TALK_STEP_MS = 400  # tramos siguientes si aún no habla
WALK_TALK_FIRST_MS = 2000  # primer tramo hacia el legendario (los Regis, ~1,8 s; chocar con él no hace nada)
QUESTION_BOX_WAIT_S = 2.5  # tras la A que abre la caja Sí/No, tiempo mínimo antes de otra A sobre la pregunta
EARLY_START_GONE_S = 2.5  # sin su texto tanto tiempo antes de la A final: el combate ya ha empezado
PRESS_TO_VIDEO_S = 0.4  # desde que se envía una pulsación hasta que su efecto se ve con seguridad en el vídeo


class RouteMixin:
    """Pasos de macro, ruta hasta el legendario y timeline final."""

    def _run_macro(self, ctx: StateContext, method: AdvanceMethod, steps) -> bool:
        """Ejecuta los pasos de una acción de avance rápido (ver ``_run_macro_steps``)."""
        return self._run_macro_steps(ctx, steps, t("avance rápido: {id}", "fast advance: {id}", id=method.id))

    def _run_macro_steps(self, ctx: StateContext, steps, label: str) -> bool:
        """Ejecuta pasos uno a uno. ``False`` si una comprobación de pantalla falla (no se sigue pulsando)."""
        for step in steps:
            if step.kind in DIALOG_KINDS:
                if not self._dialog_step(ctx, step, label):
                    return False
                continue
            if step.kind == "check":
                if not self._check_screen(ctx, step.screen, step.ms / 1000):
                    ctx.log("warning", t("{label}: no se ve «{screen}» donde debería; no se pulsa nada más",
                                         '{label}: "{screen}" is not showing where it should; nothing else is pressed',
                                         label=label, screen=screen_name(step.screen)))
                    return False
                continue
            self.d.controller.submit(step.commands(label))
            ctx.log("input", f"{step.kind} {step.button} ({label})")
            self._pass_time(ctx, step.duration_s)
        return True

    def _settled(self, ctx: StateContext, timeout_s: float, still_s: float = 0.45, after_t: float | None = None) -> Frame:
        """Primer frame en que la parte baja (textos y cajas) lleva ``still_s`` sin cambiar, en tiempo de vídeo.
        ``after_t``: se ignoran los frames anteriores (p. ej. los que aún no muestran el efecto de la última A:
        el vídeo llega con retraso y en la cola quedan frames viejos; mirarlos llevaría a pulsar «Sí» a ciegas)."""
        end = ctx.now() + timeout_s
        frame = self._next_frame(ctx)
        while after_t is not None and frame.t_s < after_t and ctx.now() < end:
            frame = self._next_frame(ctx)
        ref, since = thumbnail(frame, TEXT_REGION), frame.t_s
        while ctx.now() < end:
            frame = self._next_frame(ctx)
            cur = thumbnail(frame, TEXT_REGION)
            if changed_fraction(ref, cur) > 0.01:
                ref, since = cur, frame.t_s
            elif frame.t_s - since >= still_s:
                break
        return frame

    def _dialog_seen(self, ctx: StateContext, t_from: float, window_s: float) -> bool:
        """¿Se ve un diálogo en los frames de ``[t_from, t_from + window_s]`` (tiempo de vídeo)? Los frames
        anteriores (aún en la cola) no cuentan, y no se da por perdido hasta haber visto la ventana entera."""
        end = ctx.now() + window_s + 2.0  # tope por si dejan de llegar frames
        streak = 0
        while ctx.now() < end:
            frame = self._next_frame(ctx)
            if frame.t_s < t_from:
                continue
            if frame.t_s > t_from + window_s:
                return False
            streak = streak + 1 if dialog_visible(frame.image) else 0
            if streak >= 3:
                return True
        return False

    def _walk_talk(self, ctx: StateContext, step, label: str) -> bool:
        """Caminar hacia ``step.button`` hasta el legendario y hablarle (ver ``MacroStep``). Chocar con él no hace
        nada, así que el primer tramo es largo. Cada A va sola y se espera a ver si sale su texto ANTES de
        volver a pulsar: si el tramo siguiente (cruceta + A) ya estuviera en cola al aparecer el texto, esa A de
        más lo cerraría y el combate empezaría ~5 s antes de la A final (con un delay medido falso)."""
        end = ctx.now() + step.ms / 1000
        direction = ControllerState(dpad=DPad[step.button])
        hold_ms, tries = WALK_TALK_FIRST_MS, 0
        while ctx.now() < end:
            self.d.controller.submit([*split_long(direction, hold_ms, label), Command(NEUTRAL, 150, label)])
            self._pass_time(ctx, (hold_ms + 150) / 1000)
            self._press(ctx, Button.A, label)
            tries += 1
            if self._dialog_seen(ctx, ctx.now() + 0.1, WALK_TALK_WAIT_S):
                ctx.log("input", t("{label}: hablándole tras {tries} intentos", "{label}: talking to it after {tries} tries",
                                   label=label, tries=tries))
                return True
            hold_ms = WALK_TALK_STEP_MS
        ctx.log("warning", t("{label}: tras {tries} intentos no aparece su texto",
                             "{label}: its text did not appear after {tries} tries", label=label, tries=tries))
        return False

    def _talk_seek(self, ctx: StateContext, step, label: str) -> bool:
        """Hablar con un NPC al que se ha llegado andando hacia ``step.button`` hasta chocar, esté el personaje en su
        fila o en la de al lado (ver ``MacroStep``): A; si no sale su texto, empujar hacia los lados perpendiculares
        (el NPC o el borde lo bloquean: solo gira al personaje, nunca lo mueve) y A en cada uno."""
        end = ctx.now() + step.ms / 1000
        face = DPad[step.button]
        sides = [DPad.UP, DPad.DOWN] if face in (DPad.LEFT, DPad.RIGHT) else [DPad.LEFT, DPad.RIGHT]
        for i, d in enumerate([None, *sides]):
            if ctx.now() >= end:
                break
            if d is not None:
                self.d.controller.submit([Command(ControllerState(dpad=d), BUMP_MS, label), Command(NEUTRAL, 400, label)])
                self._pass_time(ctx, (BUMP_MS + 400) / 1000)
            self._press(ctx, Button.A, label)
            if self._dialog_seen(ctx, ctx.now() + 0.1, WALK_TALK_WAIT_S):
                if i:
                    ctx.log("input", t("{label}: hablándole tras girarse hacia {direction}",
                                       "{label}: talking to it after turning {direction}", label=label, direction=d.name))
                return True
        ctx.log("warning", t("{label}: no aparece su texto ni girándose hacia los lados",
                             "{label}: its text did not appear, not even after turning to the sides", label=label))
        return False

    def _dialog_step(self, ctx: StateContext, step, label: str) -> bool:
        """Pasos de diálogo sin leer el texto (ver ``MacroStep``). ``False`` si la pantalla no es la esperada."""
        if step.kind == "walk_talk":
            return self._walk_talk(ctx, step, label)
        if step.kind == "talk_seek":
            return self._talk_seek(ctx, step, label)
        end = ctx.now() + step.ms / 1000
        presses = 0
        plain_press_t = -math.inf  # última A sobre un texto SIN flechita ▼ (una pregunta: su caja sale con la A)
        # Solo frames que ya reflejan la última pulsación, aunque la hiciera el paso anterior (retraso de mando +
        # vídeo): con varios «dialog_next» seguidos, el texto viejo con ▼ aún en pantalla haría pulsar dos veces.
        seen_after = ctx.now() + PRESS_TO_VIDEO_S
        while ctx.now() < end:
            frame = self._settled(ctx, max(0.5, end - ctx.now()), after_t=seen_after)
            image = frame.image
            choice = choice_box_visible(image)
            text = choice or dialog_visible(image)
            if step.kind == "dialog_until_choice":
                if choice:
                    ctx.log("detection", t("{label}: caja de elección tras {presses} A",
                                           "{label}: choice box after {presses} A", label=label, presses=presses))
                    return True
                if presses and text and not dialog_arrow_visible(image):
                    # Pregunta sin su caja todavía: una A la abre. Otra A justo después contestaría «Sí» a ciegas
                    # si la caja tarda o no se reconoce en algún frame: se deja ver la imagen un buen rato antes.
                    if ctx.now() - plain_press_t < QUESTION_BOX_WAIT_S:
                        continue
                    plain_press_t = ctx.now()
                if text or presses == 0:  # la primera A abre el primer texto (hablar con el objeto)
                    self._press(ctx, Button.A, label)
                    presses += 1
                    seen_after = ctx.now() + PRESS_TO_VIDEO_S
                continue
            if not text or choice:
                continue
            arrow = dialog_arrow_visible(image)
            if step.kind == "dialog_next" and arrow:  # texto completo con más detrás
                self._press(ctx, Button.A, label)
                return True
            if step.kind == "dialog_settle" and not arrow:  # último texto (sin ▼): se queda en pantalla
                return True
        ctx.log("warning", t("{label}: no aparece el texto esperado ({kind})",
                             "{label}: the expected text did not appear ({kind})", label=label, kind=step.kind))
        return False

    def _approach_lead(self) -> float:
        """Segundos que dura la ruta hasta el legendario (se reservan antes de la primera pulsación)."""
        return self.d.encounter.approach_lead_s if self.d.encounter.approach else 0.0

    def _approach_steps(self) -> tuple[MacroStep, ...]:
        return tuple(MacroStep.from_dict(x) for x in self.d.encounter.approach)

    def _pokemon_extra(self) -> int:
        """Con reidentificación delante del legendario: llamadas que sus parpadeos (uno cada ~7,8 s de media) añaden
        durante la ruta. El plan inicial apunta antes en esa cantidad; delante del legendario se re-planifica con
        exactitud."""
        if not any(st.kind == "reidentify_pokemon" for st in self._approach_steps()):
            return 0
        if self.d.encounter.approach_front_fast_advance:
            # Lo que falte se ajusta delante del legendario; pasarse es perder el intento: se apunta por debajo del
            # máximo desvío visto (Cresselia: el barco suma entre ~16 900 y ~20 500).
            seen = max(self._extra_seen) + FRONT_EXTRA_MARGIN if self._extra_seen else 0
            return max(self.d.encounter.approach_extra_advances, seen)
        if self._extra_seen:  # lo medido en los intentos de esta ejecución manda
            return round(sum(self._extra_seen) / len(self._extra_seen))
        if self.d.encounter.approach_extra_advances:
            return self.d.encounter.approach_extra_advances
        return round(self.d.encounter.approach_lead_s / POKEMON_MEAN_INTERVAL_S)

    def _approach(self, ctx: StateContext, trigger_t: float) -> str | None:
        """Ruta hasta el legendario (``approach_lead_s`` antes de hablarle): quitar el mensaje y caminar. Si algo
        falla o no da tiempo, no se le habla: el intento se da por perdido y se reinicia el juego.

        Con un paso ``reidentify_pokemon`` la ruta va en dos partes: hasta el legendario, reidentificar allí (sus
        parpadeos gastan RNG) y re-planificar; la segunda parte empieza ``approach_after_lead_s`` antes del nuevo
        inicio del timeline. El plan nuevo queda en ``ctx.data["plan"]``."""
        self._emit(message=t("Yendo hacia el legendario: no toques el mando",
                             "Heading to the legendary: do not touch the controller"))
        ctx.log("input", t("Aproximación: empieza {lead:.1f} s antes de hablar con el legendario",
                           "Approach: starts {lead:.1f} s before talking to the legendary", lead=trigger_t - ctx.now()))
        if self._message_scene and not self._dismiss_message(ctx):
            ctx.log("error", t("No se pudo quitar el mensaje antes de la ruta: se reinicia el juego",
                               "Could not dismiss the message before the route: restarting the game"))
            return "RESET"
        steps = self._approach_steps()
        split = next((i for i, st in enumerate(steps) if st.kind == "reidentify_pokemon"), None)
        if split is not None:
            if not self._run_macro_steps(ctx, steps[:split], t("aproximación", "approach")):
                ctx.log("error", t("La ruta hacia el legendario no fue como se esperaba: se reinicia el juego",
                                   "The route to the legendary did not go as expected: restarting the game"))
                return "RESET"
            plan = self._reidentify_with_pokemon(ctx, steps[split])
            if plan is None:
                return "RESET"
            trigger_t = plan.presses[0].send_t
            self._wait_until(ctx, trigger_t - self.d.encounter.approach_after_lead_s)
            steps = steps[split + 1:]
        if not self._run_macro_steps(ctx, steps, t("aproximación", "approach")):
            ctx.log("error", t("La ruta hacia el legendario no fue como se esperaba: se reinicia el juego",
                               "The route to the legendary did not go as expected: restarting the game"))
            return "RESET"
        if ctx.now() > trigger_t - 0.2:
            ctx.log("error", t("La ruta terminó tarde ({late:+.0f} ms respecto a la pulsación): se reinicia el juego",
                               "The route finished late ({late:+.0f} ms relative to the press): restarting the game",
                               late=(ctx.now() - trigger_t) * 1000))
            return "RESET"
        return None

    def _dialog_gone_before(self, ctx: StateContext, until: float) -> float | None:
        """Vigila hasta ``until`` que el diálogo siga en pantalla. Si desaparece ``EARLY_START_GONE_S`` seguidos,
        devuelve el instante (vídeo) en que dejó de verse; si no, ``None``. Un hueco corto no cuenta: tras la A de
        inicio con Regigigas el cuadro desaparece ~1 s mientras despierta y vuelve con «¡Gigaaas!»."""
        missing_since = None
        while ctx.now() < until:
            frame = self._next_frame(ctx)
            if dialog_visible(frame.image):
                missing_since = None
                continue
            missing_since = frame.t_s if missing_since is None else missing_since
            if frame.t_s - missing_since >= EARLY_START_GONE_S:
                return missing_since
        return None

    def _reset_unless_battle(self, ctx: StateContext) -> str:
        """Tras la A de inicio, antes de reiniciar se mira si el combate ya ha empezado (p. ej. una A de más cerró
        el texto del legendario). Si es así, nunca se reinicia: se pasa a ENCOUNTER para mirar si es shiny, y el
        intento no se usa para calibrar."""
        end = ctx.now() + 1.5
        while ctx.now() < end:
            frame = self._next_frame(ctx)
            if battle_dialog_visible(frame.image) or battle_menu_visible(frame.image):
                ctx.log("warning", t("El combate ya ha empezado: no se reinicia, se mira si es shiny (el intento no "
                                     "sirve para calibrar)",
                                     "The battle has already started: not restarting, checking whether it is shiny "
                                     "(the attempt cannot be used for calibration)"))
                self._battle_possible = True
                ctx.data["early_start"] = True
                ctx.data["final_sent_t"] = frame.t_s - 1.0
                return "ENCOUNTER"
        return "RESET"

    def timeline(self, ctx: StateContext) -> str:
        """TIMELINE: tras la pulsación de inicio, ejecuta los pasos de la fase y envía la pulsación final.

        Con aproximación se exige ver el diálogo del legendario; si su texto se cierra antes de tiempo, el combate
        ya ha empezado y se pasa a ENCOUNTER marcando el intento como no válido para calibrar. Desde la A final
        ``_battle_possible`` impide cualquier reinicio automático (podría haber un shiny en pantalla).
        """
        plan: Plan = ctx.data["plan"]
        ctx.data["early_start"] = False
        self._emit(phase="TIMELINE", message=t("Timeline en marcha: no toques el mando",
                                               "Timeline running: do not touch the controller"),
                   next_action=f"{plan.final.trigger} (final)", next_action_t=plan.final.send_t)
        phase_steps = self.d.encounter.timeline[0].steps if self.d.encounter.timeline else ()
        if phase_steps:
            # Tras la A de inicio (p. ej. la que lleva a la sala del legendario): caminar hasta él y hablarle.
            steps = tuple(MacroStep.from_dict(x) for x in phase_steps)
            if not self._run_macro_steps(ctx, steps, "timeline"):
                ctx.log("error", t("Los pasos del timeline no fueron como se esperaba: no se pulsa la A final y se "
                                   "reinicia el juego",
                                   "The timeline steps did not go as expected: the final A is not pressed and the "
                                   "game is restarted"))
                return self._reset_unless_battle(ctx)
            if ctx.now() > plan.final.send_t - 1.0:
                ctx.log("error", t("Los pasos del timeline terminaron demasiado tarde: se reinicia el juego",
                                   "The timeline steps finished too late: restarting the game"))
                return self._reset_unless_battle(ctx)
        if self.d.encounter.approach:
            # Al hablarle debe aparecer su diálogo; si no, el timeline no ha empezado y la A final no serviría.
            started = self._wait_screen(ctx, lambda f: dialog_visible(f.image),
                                        max(0.5, min(4.0, plan.final.send_t - ctx.now() - 1.0)))
            if not started:
                ctx.log("error", t("No ha empezado el diálogo con el legendario (¿no estaba delante?): no se pulsa la A "
                                   "final y se reinicia el juego",
                                   "The dialog with the legendary did not start (was it not in front?): the final A "
                                   "is not pressed and the game is restarted"))
                return self._reset_unless_battle(ctx)
            ctx.log("detection", t("Diálogo con el legendario: timeline en marcha",
                                   "Dialog with the legendary: timeline running"))
            gone_t = self._dialog_gone_before(ctx, plan.final.send_t - 2.0)
            if gone_t is not None:
                # Su texto se cerró solo (una A de más): el combate ya está empezando y el avance no es el del plan.
                ctx.log("error", t("El texto del legendario se cerró antes de la A final: el combate ha empezado antes de "
                                   "tiempo. Se mira si es shiny y se captura, pero este intento no se usa para calibrar",
                                   "The legendary's text closed before the final A: the battle started early. "
                                   "It is checked for shiny and caught, but this attempt is not used for calibration"))
                ctx.data["early_start"] = True
                ctx.data["final_sent_t"] = gone_t
                self._battle_possible = True
                return "ENCOUNTER"
        ctx.sleep_until(plan.final.send_t - 2.0)
        self._check_controller(t("antes de la pulsación final", "before the final press"))
        ctx.sleep_until(plan.final.send_t)
        late = ctx.now() - plan.final.send_t
        if late > plan.final.margin_s * 0.5:
            raise InterventionRequired(t("La pulsación final llegaría {late:.0f} ms tarde: no se pulsa. "
                                         "Reinicia el juego y vuelve a empezar",
                                         "The final press would be {late:.0f} ms late: not pressing. "
                                         "Restart the game and start again", late=late * 1000))
        return self._send_final(ctx, plan.final)
