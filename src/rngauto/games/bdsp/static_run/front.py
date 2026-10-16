"""Reidentificación delante del legendario: con sus parpadeos en la escena (gastan RNG), se vuelve a situar el
estado, se re-planifica y, si falta mucho, se usa la Pokédex allí mismo antes de hablarle."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor

from rngauto.core.statemachine import InterventionRequired, StateContext
from rngauto.core.target import Target
from rngauto.i18n import t
from rngauto.vision.blink_signal import BlinkEventStream, EyeMatcher, EyeWatcher

from ..blink_pipeline import BlinkRecovery, MIN_EVENT_CONFIDENCE
from ..fast_advance import FastAdvanceChoice, MacroStep, choose_method
from ..planner import Plan, PlanningError, plan_static_in_scene
from ..recovery import RecoveryError
from ..reidentify_pokemon import certain_until, fit_period, reidentify_with_pokemon, ticks_from_blinks
from .config import Mode, POKEMON_MEAN_INTERVAL_S, fmt_duration


FRONT_MAX_EVENTS = 30  # parpadeos con los que se re-localiza delante del legendario
FRONT_REIDENTIFY_S = 150.0  # lo que tarda reidentificar delante del legendario (parpadeos de ambos)
FRONT_FAST_MAX = 6  # acciones de Pokédex delante del legendario por intento
FRONT_WAIT_MAX_S = 2400.0  # tope de espera/ajuste delante del legendario con avance rápido allí


def _front_search(rec: BlinkRecovery, events: list, blinks: int, until_t: float,
                  abs_window: tuple[int, int, float] | None, enc_window: tuple[int, int], started: float,
                  scene_rate: float):
    """Reidentificación delante del legendario (en un hilo aparte: puede tardar segundos).

    Prueba ventanas con los parpadeos más recientes (todos primero): si el juego da un tirón (observado en
    consola: dos tiradas de golpe a los ~2,5 min), lo de antes ya no encaja y vale lo posterior. El periodo se
    ajusta con los propios parpadeos (el del reloj difiere unas milésimas: en minutos, medio segundo).
    ``abs_window`` es ``(min, max, t)``: ventana absoluta ya conocida en el instante ``t`` (se ensancha con lo
    que avanza la escena después); sin ella se usa la ventana relativa del encuentro, ``enc_window``.

    Devuelve ``(encontrado | None, ventana, periodo, t0, [(n, error), ...])``.
    """
    errors: list[tuple[int, RecoveryError]] = []
    window, period, t0 = events, rec.clock.period_s, 0.0
    for n in sorted({len(events), max(blinks, len(events) - 4), blinks}, reverse=True):
        window = events[-n:]
        times = [e.t_s for e in window]
        period, t0 = fit_period(times, rec.clock.period_s)
        ticks = ticks_from_blinks(times, [e.kind for e in window], t0, period, until_t=until_t)
        if abs_window is not None:  # ya localizado (o tras la Pokédex): cerca de lo previsto
            lo = abs_window[0]
            hi = abs_window[1] + int(max(0.0, t0 - abs_window[2]) * scene_rate * 1.3)
        else:
            expected = rec.clock.advance_at(t0)
            # Con el legendario a la vista desde el principio de la ruta, el contador va por delante (~+110).
            # La ventana es del encuentro: con un viaje en medio (barco de Cresselia) el desvío es mayor.
            lo = expected + enc_window[0]
            hi = expected + enc_window[1] + int((t0 - started) / 3.0)
        try:
            return reidentify_with_pokemon(rec.result.state, ticks, period, lo, hi), window, period, t0, errors
        except (RecoveryError, ValueError) as exc:  # ValueError: la tirada 0 no cae en un parpadeo
            errors.append((n, exc))
    return None, window, period, t0, errors


class FrontMixin:
    """Reidentificación y re-planificación delante del legendario."""

    def _reidentify_with_pokemon(self, ctx: StateContext, step: MacroStep) -> Plan | None:
        """Delante del legendario: observa ``step.blinks`` parpadeos con el ojo ``step.eye``, reidentifica teniendo
        en cuenta los parpadeos del Pokémon y re-planifica el mismo objetivo sobre la escena simulada (jugador +
        Pokémon). ``None`` si no se puede (el intento se pierde y se reinicia el juego).

        El cálculo va en un hilo aparte: mientras, se siguen leyendo frames (el búfer no se llena, la PARADA se
        atiende y no se pierde ningún parpadeo)."""
        matcher = self.d.eye_matcher_for(step.eye) if self.d.eye_matcher_for else None
        if matcher is None:
            ctx.log("error", t("Falta preparar el ojo «{eye}» (delante del legendario): se reinicia el juego",
                               'The eye "{eye}" (in front of the legendary) is not set up: restarting the game',
                               eye=step.eye))
            return None
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="front-reidentify")
        try:
            return self._front_loop(ctx, step, matcher, executor)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _front_loop(self, ctx: StateContext, step: MacroStep, matcher: EyeMatcher,
                    executor: ThreadPoolExecutor) -> Plan | None:
        """Bucle de ``_reidentify_with_pokemon``: observa, lanza la búsqueda en ``executor`` y re-planifica.

        Si el objetivo aún queda lejos, se espera allí (re-localizando con cada parpadeo) o, si el encuentro lo
        permite, se usa la Pokédex y se vuelve a observar. ``None`` si se pierde el ojo, se pasa el objetivo o se
        agota el tiempo: el llamador reinicia el juego.
        """
        rec: BlinkRecovery = ctx.data["recovery"]
        target: Target = ctx.data["target"]
        self._emit(phase="REIDENTIFY", message=t("Delante del legendario: reidentificando con sus parpadeos…",
                                                 "In front of the legendary: re-identifying with its blinks…"),
                   blinks=0, blinks_needed=step.blinks, next_action="", next_action_t=None)
        if self._message_scene and not self._show_message(ctx):
            ctx.log("error", t("Delante del legendario no aparece el mensaje del profesor Serbal: se reinicia el "
                               "juego", "In front of the legendary, Professor Rowan's message does not appear: "
                               "restarting the game"))
            return None
        watcher = EyeWatcher(matcher)
        stream = BlinkEventStream(min_duration_s=self.cfg.blink_threshold_window_s,
                                  min_plateau_blinks=self.cfg.min_plateau_blinks)
        enc = self.d.encounter
        started = ctx.now()
        deadline = started + step.ms / 1000
        # Con avance rápido delante del legendario se puede tardar más (acercarse al objetivo y esperar), con tope.
        hard_deadline = started + step.ms / 1000 + (FRONT_WAIT_MAX_S if enc.approach_front_fast_advance else 0.0)
        scene_rate = 1 / rec.clock.period_s + 1 / POKEMON_MEAN_INTERVAL_S  # llamadas/s con el legendario en escena
        events: list = []
        tried = 0
        front_fast = 0
        # Ventana absoluta (llamadas desde la base) una vez localizado el estado, y desde cuándo vale.
        abs_window: tuple[int, int, float] | None = None
        job: Future | None = None
        job_until = 0.0
        last_poll = ctx.now()
        last_eye_t = ctx.now()
        while ctx.now() < deadline:
            frame = self._next_frame(ctx)
            sample = watcher.process(frame)
            watcher.samples.clear()
            stream.feed(sample)
            self._keep_awake(ctx)
            if sample.valid and sample.score >= self.cfg.scene_min_score:
                last_eye_t = ctx.now()
            elif ctx.now() - last_eye_t > self.cfg.scene_lost_s:
                ctx.log("error", t("Delante del legendario no veo el ojo desde hace {s:.0f} s: se reinicia el juego",
                                   "In front of the legendary the eye has not been visible for {s:.0f} s: "
                                   "restarting the game", s=ctx.now() - last_eye_t))
                return None
            if ctx.now() - last_poll < 0.5:
                continue
            last_poll = ctx.now()
            events.extend(e for e in stream.poll() if e.confidence >= MIN_EVENT_CONFIDENCE and e.t_s > started)
            self._emit(blinks=min(len(events), step.blinks), last_score=sample.score)
            if job is None:
                if len(events) < step.blinks or len(events) == tried:
                    continue
                tried = len(events)
                events = events[-FRONT_MAX_EVENTS:]  # basta con los últimos (y el cálculo no crece sin límite)
                until_t = job_until = frame.t_s - 1.0
                ctx.log("recovery", t("Datos de la reidentificación con el legendario",
                                      "Re-identification data with the legendary"), data={
                    "base": str(rec.result.state), "period_s": rec.clock.period_s, "started": started,
                    "blinks": [[e.t_s, None if e.kind is None else int(e.kind), e.confidence] for e in events],
                    "until_t": until_t, "clock": [rec.clock.anchor_t, rec.clock.anchor_advance,
                                                  rec.clock.period_s, rec.clock.calls_per_tick]})
                job = executor.submit(_front_search, rec, list(events), step.blinks, until_t, abs_window,
                                      enc.approach_reidentify_window, started, scene_rate)
                continue
            if not job.done():
                continue
            found, window, period, t0, errors = job.result()
            job = None
            for n, exc in errors:
                ctx.log("recovery", t("Reidentificación con el legendario (últimos {n} parpadeos): {exc}",
                                      "Re-identification with the legendary (last {n} blinks): {exc}", n=n, exc=exc))
            if found is None:
                continue
            events = [e for e in events if e.t_s >= window[0].t_s]  # lo anterior a un posible tirón ya no sirve
            tried = len(events)
            idle = found.simulator(t0, period)
            idle.run_until(ctx.now())
            # Adelanto que tendrá el contador al hablarle (lo usa el plan de arriba en los próximos intentos).
            now_advance = rec.clock.advance_at(job_until + 1.0)
            if front_fast == 0 and abs_window is None:  # solo la ruta (sin lo que consuma la Pokédex de aquí)
                self._extra_seen = (self._extra_seen + [found.end_advance - now_advance + round(
                    enc.approach_after_lead_s / POKEMON_MEAN_INTERVAL_S) + 2])[-5:]
            horizon = certain_until(idle.fork(), t0, found, until_s=ctx.now() + 600.0)
            half = (found.pokemon_window_s[1] - found.pokemon_window_s[0]) / 2
            ctx.log("recovery", t("Reidentificado delante del legendario con {n} parpadeos "
                                  "({mismatches} vistos distinto): advance "
                                  "{advance} (+{extra} por sus parpadeos); "
                                  "su siguiente parpadeo ±{half:.2f} s; escena fiable {reliable:.0f} s más",
                                  "Re-identified in front of the legendary with {n} blinks "
                                  "({mismatches} seen differently): advance "
                                  "{advance} (+{extra} from its blinks); "
                                  "its next blink ±{half:.2f} s; scene reliable for {reliable:.0f} s more",
                                  n=len(window), mismatches=found.mismatches, advance=found.end_advance,
                                  extra=found.end_advance - now_advance, half=half,
                                  reliable=horizon - ctx.now()))
            try:
                plan = plan_static_in_scene(idle, horizon, enc.timeline, enc.final,
                                            target.advance, final_delay=ctx.data["delay"], now_s=ctx.now(),
                                            prep_s=enc.approach_after_lead_s + 1.0, lead_s=self.cfg.lead_s)
            except PlanningError as exc:
                remaining = target.advance - ctx.data["delay"] - idle.advance  # llamadas hasta la A final
                if remaining < 0:
                    ctx.log("warning", t("Delante del legendario el contador ({current}) ya ha pasado el objetivo "
                                         "{target}: se reinicia el juego",
                                         "In front of the legendary the counter ({current}) is already past the "
                                         "target {target}: restarting the game",
                                         current=idle.advance, target=target.advance))
                    return None
                ctx.log("recovery", t("Delante del legendario faltan {remaining} avances para el objetivo "
                                      "{target} ({exc})",
                                      "In front of the legendary, {remaining} advances left until the target "
                                      "{target} ({exc})", remaining=remaining, target=target.advance, exc=exc))
                choice = None
                if enc.approach_front_fast_advance and front_fast < FRONT_FAST_MAX:
                    choice = self._front_fast_choice(remaining, scene_rate)
                if choice is not None:
                    front_fast += 1
                    before = idle.advance
                    if not self._front_fast_advance(ctx, choice, matcher):
                        return None
                    # Volver a observar desde cero, buscando cerca de lo que consume la acción. El umbral del
                    # ojo ya está aprendido: se conserva (si no, habría que esperar a calibrarlo de nuevo).
                    lo = before + max(0, int(choice.expected - 4 * choice.sd))
                    hi = before + int(choice.expected + 4 * choice.sd) + 300
                    abs_window = (lo, hi, ctx.now())
                    stream.freeze()
                    stream = BlinkEventStream(fixed_threshold=stream.threshold,
                                              min_duration_s=self.cfg.blink_threshold_window_s,
                                              min_plateau_blinks=self.cfg.min_plateau_blinks)
                    events, tried, started = [], 0, ctx.now()
                    last_eye_t = ctx.now()
                    deadline = min(hard_deadline, max(deadline, ctx.now() + step.ms / 1000))
                    self._emit(phase="REIDENTIFY",
                               message=t("Delante del legendario: reidentificando tras la Pokédex…",
                                         "In front of the legendary: re-identifying after the Pokédex…"),
                               blinks=0, blinks_needed=step.blinks, next_action="", next_action_t=None)
                    continue
                # Sin Pokédex: si esperando no se llega a tiempo, no tiene sentido seguir aquí.
                if remaining / scene_rate > hard_deadline - ctx.now():
                    ctx.log("warning", t("Delante del legendario faltan {wait} para el objetivo y no hay tiempo "
                                         "(ni avance rápido): se reinicia el juego",
                                         "In front of the legendary, {wait} left until the target and there is no "
                                         "time (nor fast advance): restarting the game",
                                         wait=fmt_duration(remaining / scene_rate)))
                    return None
                # Esperar aquí: con cada parpadeo se re-localiza (ventana estrecha) y la escena fiable avanza.
                abs_window = (found.first_advance - 30, found.end_advance + 300, job_until + 1.0)
                deadline = min(hard_deadline, max(deadline, ctx.now() + 120))
                continue
            ctx.data["plan"] = plan
            ctx.log("plan", t("Plan delante del legendario:\n", "Plan in front of the legendary:\n")
                    + plan.describe(self._fmt_t))
            first = plan.presses[0]
            self._emit(next_action=f"{first.trigger} ({first.label})", next_action_t=first.send_t,
                       message=t("Reidentificado delante del legendario: esperando el momento exacto",
                                 "Re-identified in front of the legendary: waiting for the exact moment"))
            return plan
        ctx.log("error", t("No se pudo reidentificar delante del legendario a tiempo: se reinicia el juego",
                           "Could not re-identify in front of the legendary in time: restarting the game"))
        return None

    def _front_fast_choice(self, remaining: int, scene_rate: float) -> FastAdvanceChoice | None:
        """Acción de Pokédex delante del legendario que deja margen para reidentificar y hacer la ruta final."""
        methods = [m for m in self.d.fast_methods if m.id in self.d.encounter.fast_advance
                   and m.id not in self._fast_disabled]
        if not methods or self.cfg.mode is Mode.DRY_RUN:
            return None
        after = self.d.encounter.approach_after_lead_s + FRONT_REIDENTIFY_S
        return choose_method(remaining, scene_rate, methods, self.d.fast_stats, after_s=after,
                             min_gain_s=self.cfg.fast_advance_min_gain_s, k_sigma=self.cfg.fast_advance_k_sigma)

    def _front_fast_advance(self, ctx: StateContext, choice: FastAdvanceChoice, matcher: EyeMatcher) -> bool:
        """Ejecuta la acción delante del legendario y vuelve a dejar la escena como estaba (comprobado con la
        imagen). ``False`` si el menú no se comportó como se esperaba o no se recupera la escena (se reinicia el
        juego: nunca se sigue pulsando a ciegas)."""
        m = choice.method
        self._emit(phase="FAST_ADVANCE", message=t("Delante del legendario: {name} (no toques el mando)",
                                                   "In front of the legendary: {name} (do not touch the controller)",
                                                   name=m.name))
        ctx.log("input", t("Avance rápido delante del legendario: {choice}",
                           "Fast advance in front of the legendary: {choice}", choice=choice.describe()))
        try:
            reference = self._scene_reference(ctx, matcher)
        except InterventionRequired:
            ctx.log("error", t("Delante del legendario no veo el ojo: no se abre la Pokédex y se reinicia el juego",
                               "In front of the legendary the eye is not visible: the Pokédex is not opened and "
                               "the game is restarted"))
            return False
        ok = (not self._message_scene) or self._dismiss_message(ctx)
        ok = ok and self._run_macro(ctx, m, choice.steps)
        if not ok:
            self._fast_failed(m.id)
            ctx.log("error", t("«{name}» delante del legendario no fue como se esperaba: se reinicia el juego",
                               '"{name}" in front of the legendary did not go as expected: restarting the game',
                               name=m.name))
            return False
        if not self._restore_scene(ctx, reference, matcher=matcher):
            ctx.log("error", t("Delante del legendario no consigo volver a la escena tras «{name}»: se reinicia el "
                               "juego", 'In front of the legendary, cannot get back to the scene after "{name}": '
                               "restarting the game", name=m.name))
            return False
        return True
