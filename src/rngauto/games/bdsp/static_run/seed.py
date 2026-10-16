"""Fases de semilla y espera: COLLECT (parpadeos → estado), PLAN (objetivo y plan), FAST_ADVANCE (Pokédex),
REIDENTIFY (reidentificar tras la Pokédex) y WAIT_TRIGGER (esperar y pulsar en el centro del periodo)."""
from __future__ import annotations

import math
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from rngauto.control.controller import Button
from rngauto.core.statemachine import InterventionRequired, StateContext
from rngauto.core.target import Target
from rngauto.i18n import fmt_int, t
from rngauto.vision.blink_signal import BlinkEventStream, EyeWatcher, MIN_EVENT_SEPARATION_S

from ..blink_pipeline import (
    BlinkPredictor, BlinkRecovery, MIN_EVENT_CONFIDENCE, period_information, recover_from_events, refine_clock,
)
from ..encounters import target_label
from ..fast_advance import AdvanceMethod, FastAdvanceChoice, REIDENTIFY_MAX_BLINKS, choose_method, reidentify_events
from ..identify import ambiguity
from ..planner import Plan, PlanningError, plan_static
from ..recovery import AmbiguousPattern, RecoveryError
from .config import Mode, fmt_duration


SEARCH_CHUNK = 20_000  # advances por tramo al buscar objetivos (estado PLAN)
# Con este número de parpadeos la solución casi siempre es única (y se admiten hasta 2 parpadeos dudosos
# descartados). Se intenta antes (``min_blinks`` del encuentro, p. ej. 33) en modo estricto: sin descartes.
FULL_BLINKS = 40


class SeedMixin:
    """Semilla, plan, avance rápido con la Pokédex y espera hasta la pulsación."""

    def _fast_choice(self, ctx: StateContext, rec: BlinkRecovery, plan: Plan, current: int) -> FastAdvanceChoice | None:
        """¿Avance rápido antes de esperar? Solo con mando real y si la espera natural es larga."""
        enc = self.d.encounter
        if not self.cfg.fast_advance or self._fast_count >= self.cfg.max_fast_advances:
            return None
        methods = [m for m in self.d.fast_methods if m.id in enc.fast_advance and m.id not in self._fast_disabled]
        if not methods:
            return None
        clock = rec.clock
        rate = clock.calls_per_tick / clock.period_s
        remaining = plan.presses[0].expected_count - current
        # Tras la acción y la reidentificación (su duración se aprende) tiene que quedar tiempo para planificar y
        # hacer la ruta hasta el legendario; sin contar la ruta, el objetivo podría quedar demasiado cerca y habría
        # que pasar al siguiente.
        after = (self.cfg.prep_s + (self.cfg.approval_s if self.cfg.mode is Mode.ASSISTED else 0.0)
                 + self._approach_lead() + 15.0)
        choice = choose_method(remaining, rate, methods, self.d.fast_stats, after_s=after,
                               min_gain_s=self.cfg.fast_advance_min_gain_s, k_sigma=self.cfg.fast_advance_k_sigma)
        if choice is None:
            return None
        if self.cfg.mode is Mode.DRY_RUN:
            ctx.log("plan", t("ENSAYO: con mando se usaría el avance rápido ({choice})",
                              "DRY RUN: with a controller, fast advance would be used ({choice})",
                              choice=choice.describe()))
            return None
        if self.cfg.mode is Mode.ASSISTED and not self._fast_approved:
            ok = self.d.operator.confirm(t(
                "Faltan unos {wait} de espera para el objetivo.\n\n"
                "¿Uso la Pokédex para adelantar el RNG? El programa abrirá el menú (X), irá a la Pokédex (pasando "
                "por el mapa, esté donde esté el cursor), la recorrerá y la cerrará; después reidentificará el "
                "estado con unos parpadeos.\n\nNo toques el mando mientras tanto.",
                "About {wait} of waiting left until the target.\n\n"
                "Use the Pokédex to advance the RNG faster? The program will open the menu (X), go to the Pokédex "
                "(through the map, wherever the cursor is), scroll through it and close it; then it will "
                "re-identify the state with a few blinks.\n\nDo not touch the controller in the meantime.",
                wait=fmt_duration(remaining / rate)),
                timeout_s=120.0)
            ctx.check()
            if ok is False:
                self._fast_disabled.update(m.id for m in methods)
                self._fast_declined = True
                ctx.log("plan", t("Avance rápido rechazado: se espera de forma normal",
                                  "Fast advance declined: waiting normally"))
                return None
            if ok is None:
                return None
            self._fast_approved = True
        return choice

    def collect(self, ctx: StateContext) -> str:
        """Parpadeos → estado provisional (en segundo plano) → confirmación con parpadeos posteriores.

        La recuperación se ejecuta en otro hilo para no dejar de leer frames. Un estado provisional se
        confirma cuando los siguientes ``confirmation_blinks`` parpadeos coinciden con su predicción
        (un estado falso casi nunca acierta varios seguidos). Si algún parpadeo malo impide resolver, se prueba con una
        ventana de los más recientes.
        """
        scene = self.d.encounter.blink_scene
        self._advance_offset = 0  # estado nuevo: los advances vuelven a contarse desde su base
        self._emit(phase="COLLECT", message=t("Observando los parpadeos del personaje",
                                              "Watching your character's blinks"), blinks=0, seed_desc="",
                   blinks_needed=scene.min_blinks, provisional=False, state_hex="", clock=None,
                   target_advance=None, target_desc="", next_action="", next_action_t=None, advance_offset=0)
        self._wait_for_scene(ctx)
        if self._message_scene and not self._show_message(ctx):  # si se puede, se reinicia solo (``_on_failure``)
            raise InterventionRequired(t("No consigo que aparezca el mensaje del profesor Serbal con la caña: comprueba "
                                         "que la caña está registrada y su dirección en la rueda de «+»",
                                         "Cannot get Professor Rowan's message to appear with the rod: check that "
                                         'the rod is registered and its direction on the "+" wheel'))
        watcher = EyeWatcher(self.d.matcher)
        stream = BlinkEventStream(min_duration_s=self.cfg.blink_threshold_window_s,
                                  min_plateau_blinks=self.cfg.min_plateau_blinks)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery")
        job: Future | None = None
        job_size = 0
        job_last_t = -math.inf
        failures = 0
        provisional: BlinkRecovery | None = None
        predictor: BlinkPredictor | None = None
        confirmations: list[tuple[int, float]] = []
        seen_t = -math.inf
        last_try = ctx.now()
        collect_started = ctx.now()
        try:
            while True:
                frame = self._next_frame(ctx)
                sample = watcher.process(frame)
                watcher.samples.clear()  # el stream ya conserva lo necesario
                stream.feed(sample)
                self._keep_awake(ctx)
                now = ctx.now()
                if now - last_try < 1.0:
                    continue
                last_try = now
                events = stream.all_events()
                calibrating = stream.threshold is None
                if provisional is not None:
                    shown, needed = len(confirmations), scene.confirmation_blinks
                    message = t("Estado encontrado; confirmándolo con un par de parpadeos más",
                                "State found; confirming it with a couple more blinks")
                elif calibrating:
                    shown, needed = stream.preview_count(), scene.min_blinks
                    message = t("Contando parpadeos (≈{shown} de {needed}). Mientras, el detector aprende "
                                "cómo parpadea tu personaje",
                                "Counting blinks (≈{shown} of {needed}). Meanwhile, the detector is learning "
                                "how your character blinks", shown=shown, needed=scene.min_blinks)
                elif len(events) < scene.min_blinks:
                    shown, needed = len(events), scene.min_blinks
                    message = t("Contando parpadeos: {shown} de {needed} (cada ~4-12 s; no toques nada)",
                                "Counting blinks: {shown} of {needed} (one every ~4-12 s; do not touch anything)",
                                shown=shown, needed=needed)
                else:
                    shown, needed = len(events), max(scene.min_blinks, len(events))
                    message = t("Calculando el estado del RNG con {shown} parpadeos (si no basta, se usa el siguiente)",
                                "Calculating the RNG state with {shown} blinks (if not enough, the next one is used)",
                                shown=shown)
                self._emit(blinks=shown, blinks_needed=needed, last_score=sample.score, threshold=stream.threshold,
                           calibrating=calibrating, phase_elapsed_s=now - collect_started,
                           calibration_window_s=self.cfg.blink_threshold_window_s, message=message)

                if provisional is not None:
                    for e in (e for e in events if e.t_s > seen_t + MIN_EVENT_SEPARATION_S):
                        seen_t = e.t_s
                        if e.confidence < MIN_EVENT_CONFIDENCE:
                            continue
                        if predictor.check(e.t_s, e.kind):
                            confirmations.append((predictor.roll_at(e.t_s), e.t_s))
                            continue
                        ctx.log("warning", t("Un parpadeo no coincide con el estado provisional: se descarta",
                                             "A blink does not match the provisional state: discarding it"))
                        provisional, predictor, confirmations = None, None, []
                        failures += 1
                        break
                    if provisional is not None and len(confirmations) >= scene.confirmation_blinks:
                        clock = refine_clock(provisional, confirmations)
                        rec = BlinkRecovery(provisional.result, provisional.assignment, clock,
                                            provisional.events_used, provisional.events_discarded)
                        stream.freeze()
                        self.last_recovery = rec
                        predictor_final = BlinkPredictor(rec)
                        predictor_final.use_clock(rec.clock)
                        ctx.data.update(recovery=rec, predictor=predictor_final, stream=stream, watcher=watcher)
                        ctx.log("recovery", t("Estado confirmado con {n} parpadeos más: {desc}",
                                              "State confirmed with {n} more blinks: {desc}",
                                              n=len(confirmations), desc=rec.describe()),
                                confidence=rec.result.confidence)
                        self._emit(provisional=False, clock=rec.clock, state_hex=rec.result.state.to_hex32(),
                                   confidence=rec.result.confidence,
                                   message=t("Estado RNG confirmado", "RNG state confirmed"),
                                   seed_desc=t("Confirmada ✓ ({used} parpadeos + {extra})",
                                               "Confirmed ✓ ({used} blinks + {extra})",
                                               used=rec.events_used, extra=len(confirmations)))
                        return "PLAN"
                    continue

                if job is not None and job.done():
                    try:
                        rec = job.result()
                    except RecoveryError as exc:
                        if job_size >= FULL_BLINKS:  # con menos, lo normal es que aún falte información
                            failures += 1
                        ctx.log("recovery", t("Aún sin solución con {n} parpadeos: {exc}",
                                              "No solution yet with {n} blinks: {exc}", n=job_size, exc=exc),
                                data={"progress": True})
                    else:
                        provisional, predictor, confirmations = rec, BlinkPredictor(rec), []
                        seen_t = job_last_t
                        ctx.log("recovery", t("Estado provisional: {desc}", "Provisional state: {desc}",
                                              desc=rec.describe()), confidence=rec.result.confidence)
                        self._emit(provisional=True, state_hex=rec.result.state.to_hex32(),
                                   confidence=rec.result.confidence,
                                   seed_desc=t("Comprobando… ({n} parpadeos)", "Checking… ({n} blinks)",
                                               n=rec.events_used))
                    job = None
                    continue

                if job is None and len(events) >= scene.min_blinks and len(events) != job_size:
                    window = events
                    if failures >= 2 and len(events) > FULL_BLINKS + 8:
                        window = events[-(FULL_BLINKS + 4):]  # ventana deslizante sin los primeros
                    # Con menos de FULL_BLINKS, solo soluciones sin descartar parpadeos: con descartes y pocos datos
                    # pueden salir semillas únicas pero falsas (visto con 33-34 parpadeos en grabaciones reales).
                    job = executor.submit(recover_from_events, window, scene.scene.npc,
                                          max_exclusions=0 if len(window) < FULL_BLINKS else 2)
                    job_size, job_last_t = len(events), window[-1].t_s
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def plan(self, ctx: StateContext) -> str:
        """PLAN: primer objetivo que cumple los criterios y tiene un plan viable con el delay estimado.

        Si falta mucho, pasa a FAST_ADVANCE; si no hay objetivo planificable, a RESET (otro estado RNG). En modo
        asistido el plan se aprueba antes; si la aprobación llega tarde, se recalcula con más preparación.
        """
        rec: BlinkRecovery = ctx.data["recovery"]
        enc = self.d.encounter
        estimate = self.d.calibration.estimate_final_delay()
        delay = estimate.value
        bump = self.d.calibration.target_bump() if self.cfg.calibration_bump else 0
        self._emit(phase="PLAN", message=t("Buscando objetivo y calculando el plan",
                                           "Searching for a target and calculating the plan"),
                   delay_desc=estimate.describe())
        prep = (self.cfg.prep_s + (self.cfg.approval_s if self.cfg.mode is Mode.ASSISTED else 0.0)
                + self._approach_lead())
        for _round in range(5):  # se re-planifica si el usuario tarda en aprobar
            now = ctx.now()
            current = rec.clock.advance_at(now)
            start = current + delay + 50
            chosen = None
            last_error = ""
            found_any = False
            # Búsqueda por tramos: para objetivos raros (shiny) el primero suele estar en los primeros miles
            # de advances; no hace falta generar 200.000 antes de empezar a planificar.
            for lo in range(start, start + self.cfg.max_search_advances, SEARCH_CHUNK):
                ctx.check()
                hi = min(lo + SEARCH_CHUNK - 1, start + self.cfg.max_search_advances)
                for cand in self.d.finder.search(rec.result.state, self.cfg.criteria, lo, hi,
                                                 limit=self.cfg.max_plan_tries):
                    found_any = True
                    goal = cand.advance + bump if bump else cand.advance
                    try:
                        plan = plan_static(rec.result.state, rec.clock, enc.blink_scene.scene, enc.timeline,
                                           enc.final, goal - self._pokemon_extra(), final_delay=delay, now_s=now,
                                           prep_s=prep, lead_s=self.cfg.lead_s)
                    except PlanningError as exc:
                        last_error = str(exc)
                        continue
                    chosen = (plan, Target(goal, self.d.finder.generate(rec.result.state, goal)) if bump else cand)
                    break
                if chosen is not None:
                    break
            if chosen is None:
                why = (t("ningún objetivo es planificable ({error})", "no target can be planned ({error})",
                         error=last_error) if found_any else
                       t("ningún advance de los próximos {n} cumple los criterios",
                         "no advance in the next {n} meets the criteria", n=fmt_int(self.cfg.max_search_advances)))
                ctx.log("warning", t("Con este estado RNG {why}: se reinicia el juego para obtener otro",
                                     "With this RNG state {why}: restarting the game to get another one", why=why))
                return "RESET"
            plan, target = chosen
            first = plan.presses[0]
            amb = ambiguity(self.d.finder, rec.result.state, target.advance, list(self.cfg.observe_fields), radius=3)
            ctx.data.update(plan=plan, target=target, delay=delay)
            ctx.log("plan", (
                t("Advance actual {current} | objetivo {target} ({traits}) | "
                  "faltan {left} | {estimate} | "
                  "margen final ±{margin:.0f} ms | ambigüedad ±3: {amb}",
                  "Current advance {current} | target {target} ({traits}) | "
                  "{left} left | {estimate} | "
                  "final margin ±{margin:.0f} ms | ambiguity ±3: {amb}",
                  current=current, target=target.advance, traits=target.traits.describe(),
                  left=target.advance - current, estimate=estimate.describe(),
                  margin=plan.final.margin_s * 1000, amb=amb)
                + (t(" | desplazamiento de desempate {bump:+d}", " | tie-break shift {bump:+d}", bump=bump)
                   if bump else "")
            ), advance=current, data={"plan": plan.describe(self._fmt_t)})
            self._emit(target_advance=target.advance,
                       target_desc=target_label(self.d.encounter, target.traits),
                       next_action=f"{first.trigger} ({first.label})", next_action_t=first.send_t,
                       message=t("Plan listo", "Plan ready"))
            choice = self._fast_choice(ctx, rec, plan, current)
            if choice is not None:
                ctx.data["fast_choice"] = choice
                ctx.log("plan", t("Falta mucho: avance rápido con «{choice}»",
                                  'Long wait ahead: fast advance with "{choice}"', choice=choice.describe()))
                return "FAST_ADVANCE"
            self.d.operator.notify(plan.describe(self._fmt_t))
            if self.cfg.mode is not Mode.ASSISTED:
                return "WAIT_TRIGGER"
            wait_s = first.send_t - ctx.now()
            ok = self.d.operator.confirm(
                t("Objetivo: {label} (advance {advance})\n"
                  "Faltan unos {wait} para la primera pulsación ({trigger}).\n"
                  "El programa pulsará SOLO en los instantes exactos; no toques el mando.\n\n"
                  "¿Ejecutar este plan?",
                  "Target: {label} (advance {advance})\n"
                  "About {wait} left until the first press ({trigger}).\n"
                  "The program will press ONLY at the exact moments; do not touch the controller.\n\n"
                  "Run this plan?",
                  label=target_label(self.d.encounter, target.traits), advance=target.advance,
                  wait=fmt_duration(wait_s), trigger=first.trigger),
                timeout_s=max(5.0, first.send_t - ctx.now() - 1.0),  # el diálogo se cierra solo si ya no da tiempo
            )
            ctx.check()  # PARADA mientras el diálogo estaba abierto
            if ok is False:
                raise InterventionRequired(t("Plan rechazado por el usuario", "Plan rejected by the user"),
                                           recoverable=False)
            if ok is True and ctx.now() < first.send_t - 1.0:
                return "WAIT_TRIGGER"
            ctx.log("warning", t("La aprobación llegó tarde para ese objetivo: se recalcula",
                                 "The approval came too late for that target: recalculating"))
            prep += 30.0
        raise InterventionRequired(t("No se aprobó ningún plan a tiempo", "No plan was approved in time"),
                                   recoverable=False)

    def fast_advance(self, ctx: StateContext) -> str:
        """FAST_ADVANCE: ejecuta una acción de avance rápido (Pokédex) y deja la escena como estaba.

        Si una pantalla no aparece, la acción se cancela y se cierra lo abierto; en ambos casos se pasa a
        REIDENTIFY, porque el RNG ha avanzado una cantidad desconocida.
        """
        choice: FastAdvanceChoice = ctx.data.pop("fast_choice")
        rec: BlinkRecovery = ctx.data["recovery"]
        m = choice.method
        self._emit(phase="FAST_ADVANCE", message=t("Avance rápido: {name} (no toques el mando)",
                                                   "Fast advance: {name} (do not touch the controller)", name=m.name),
                   next_action="", next_action_t=None)
        self._check_controller(t("antes del avance rápido", "before the fast advance"))
        reference = self._scene_reference(ctx)
        t_start = ctx.now()
        adv_before = rec.clock.advance_at(t_start)
        ctx.log("input", t("Avance rápido «{name}» desde el advance {advance} ({choice})",
                           'Fast advance "{name}" from advance {advance} ({choice})',
                           name=m.name, advance=adv_before, choice=choice.describe()),
                advance=adv_before)
        ok = (not self._message_scene) or self._dismiss_message(ctx)
        ok = ok and self._run_macro(ctx, m, choice.steps)
        t_end = ctx.now()
        if not ok:
            self._fast_failed(m.id)
            ctx.log("warning", t("No se reconoció la pantalla esperada: se cancela «{name}» y se cierra el menú; "
                                 "se sigue esperando de forma normal.",
                                 'The expected screen was not recognized: cancelling "{name}" and closing the menu; '
                                 "waiting normally instead.", name=m.name))
            self.d.operator.notify(t("Avance rápido cancelado: el juego no mostró la pantalla esperada (menú o "
                                     "Pokédex). Se sigue esperando de forma normal; revisa los detalles técnicos.",
                                     "Fast advance cancelled: the game did not show the expected screen (menu or "
                                     "Pokédex). Waiting normally instead; check the technical details."))
        if not self._restore_scene(ctx, reference):
            raise InterventionRequired(t("Después del avance rápido no consigo volver a la escena (¿hay un menú "
                                         "abierto?). Ciérralo con B y vuelve a empezar",
                                         "Cannot get back to the scene after the fast advance (is a menu "
                                         "open?). Close it with B and start again"))
        self._fast_count += 1
        ctx.data["fast_pending"] = (m if ok else None, adv_before, t_start, t_end, ctx.now(), choice)
        return "REIDENTIFY"

    def reidentify(self, ctx: StateContext) -> str:
        """Tras un avance rápido: ~7 parpadeos bastan para saber en qué advance está el juego (se busca el
        patrón cerca de lo esperado) y se confirma con un par de parpadeos más antes de planificar."""
        method, adv_before, t_start, t_end, t_back, choice = ctx.data.pop("fast_pending")
        rec: BlinkRecovery = ctx.data["recovery"]
        stream: BlinkEventStream = ctx.data["stream"]
        watcher: EyeWatcher = ctx.data["watcher"]
        npc = rec.result.npc
        period = rec.clock.period_s
        rate = rec.clock.calls_per_tick / period
        weight = period_information(rec, [])
        need = self.cfg.reidentify_blinks
        self._emit(phase="REIDENTIFY", message=t("Reidentificando el estado con unos parpadeos…",
                                                 "Re-identifying the state with a few blinks…"), blinks=0,
                   blinks_needed=need, clock=None, next_action="", next_action_t=None)
        stream.poll()  # lo anterior (incluido lo visto con el menú abierto) no sirve
        events: list = []
        tried_with = 0
        wide = method is None
        found = None
        predictor: BlinkPredictor | None = None
        confirmed = 0
        last_eye_t = ctx.now()
        deadline = ctx.now() + self.cfg.reidentify_timeout_s
        last_poll = ctx.now()
        while ctx.now() < deadline:
            sample = watcher.process(self._next_frame(ctx))
            watcher.samples.clear()
            stream.feed(sample)
            self._keep_awake(ctx)
            if sample.valid and sample.score >= self.cfg.scene_min_score:
                last_eye_t = ctx.now()
            elif ctx.now() - last_eye_t > self.cfg.scene_lost_s:
                ctx.log("error", t("No veo el ojo mientras reidentifico: se vuelve a observar desde cero",
                                   "Cannot see the eye while re-identifying: observing again from scratch"))
                return "COLLECT"
            if ctx.now() - last_poll < 0.5:
                continue
            last_poll = ctx.now()
            # Solo parpadeos vistos ya de vuelta en la escena (los de antes o con el menú abierto no valen).
            new = [e for e in stream.poll() if e.confidence >= MIN_EVENT_CONFIDENCE and e.t_s > t_back]
            if not new:
                continue
            if found is not None:  # confirmando
                for e in new:
                    if predictor.check(e.t_s, e.kind):
                        confirmed += 1
                    else:
                        ctx.log("warning", t("Un parpadeo no coincide con la reidentificación: se repite con más",
                                             "A blink does not match the re-identification: retrying with more"))
                        found, predictor, confirmed = None, None, 0
                        need = min(REIDENTIFY_MAX_BLINKS, len(events) + 3)
                        break
                events.extend(new)
                if found is not None and confirmed >= self.cfg.reidentify_confirm_blinks:
                    return self._reidentified(ctx, found, method, adv_before, t_end, events[0].t_s, choice, rate)
                continue
            events.extend(new)
            self._emit(blinks=min(len(events), need), blinks_needed=need, last_score=sample.score)
            if len(events) < need or len(events) == tried_with:
                continue
            tried_with = len(events)
            natural = rate * (events[0].t_s - t_start) + 50
            if wide:
                lo, hi = adv_before, adv_before + natural + 30_000
            else:
                lo = adv_before + max(0.0, choice.expected - 4 * choice.sd)
                hi = adv_before + natural + choice.expected + 4 * choice.sd + 300
            try:
                found = reidentify_events(rec.result.state, events, npc, period, int(lo), int(hi), weight)
            except RecoveryError as exc:
                ctx.log("recovery", t("Reidentificación con {n} parpadeos: {exc}",
                                      "Re-identification with {n} blinks: {exc}", n=len(events), exc=exc))
                if isinstance(exc, AmbiguousPattern):
                    need = min(REIDENTIFY_MAX_BLINKS, len(events) + 3)
                elif not wide:
                    wide = True  # quizá consumió mucho más o menos de lo previsto: ventana amplia
                    tried_with = 0
                else:
                    need = min(REIDENTIFY_MAX_BLINKS, len(events) + 2)
                if len(events) >= REIDENTIFY_MAX_BLINKS:
                    ctx.log("warning", t("No se pudo reidentificar: se recupera el estado desde cero",
                                         "Could not re-identify: recovering the state from scratch"))
                    return "COLLECT"
                continue
            predictor = BlinkPredictor(found.recovery)
            predictor.use_clock(found.recovery.clock)
            confirmed = 0
            ctx.log("recovery", t("Reidentificado con {n} parpadeos", "Re-identified with {n} blinks",
                                  n=found.blinks_used)
                    + (f" ({found.note})" if found.note else "") + t("; confirmando…", "; confirming…"))
            self._emit(message=t("Estado reidentificado; confirmándolo con un par de parpadeos más",
                                 "State re-identified; confirming it with a couple more blinks"),
                       seed_desc=t("Comprobando… (reidentificada con {n} parpadeos)",
                                   "Checking… (re-identified with {n} blinks)", n=found.blinks_used))
        ctx.log("warning", t("La reidentificación tarda demasiado: se recupera el estado desde cero",
                             "Re-identification is taking too long: recovering the state from scratch"))
        return "COLLECT"

    def _reidentified(self, ctx: StateContext, found, method: AdvanceMethod | None, adv_before: int, t_end: float,
                      t_first: float, choice: FastAdvanceChoice, rate: float) -> str:
        """Adopta el estado reidentificado y aprende lo que consumió la acción.

        Consumo = advance reidentificado − advance previo − lo que avanzó solo entre el fin de la acción y el
        primer parpadeo usado. Si es casi nulo, la acción probablemente no se ejecutó y se deja de usar.
        """
        new_rec: BlinkRecovery = found.recovery
        self.d.fast_stats.add_reidentify(ctx.now() - t_end)  # cuánto tarda de verdad en esta escena
        consumed = found.start_advance - adv_before - rate * max(0.0, t_first - t_end)
        if method is not None:
            if consumed < 0.25 * choice.expected:
                self._fast_failed(method.id)
                ctx.log("warning", t("«{name}» apenas ha avanzado el RNG ({consumed:.0f}): se deja de usar "
                                     "(¿se abrió de verdad la Pokédex?)",
                                     '"{name}" barely advanced the RNG ({consumed:.0f}): no longer used '
                                     "(did the Pokédex really open?)", name=method.name, consumed=consumed))
            else:
                self.d.fast_stats.add(method.id, int(round(consumed)), choice.hold_s or 0.0)
        self._advance_offset += found.start_advance
        predictor = BlinkPredictor(new_rec)
        predictor.use_clock(new_rec.clock)
        ctx.data.update(recovery=new_rec, predictor=predictor)
        self.last_recovery = new_rec
        action = (t("acción {name} ≈ {consumed:.0f}", "action {name} ≈ {consumed:.0f}", name=method.name,
                    consumed=consumed) if method else t("acción cancelada", "action cancelled"))
        ctx.log("recovery", t("Reidentificado: +{gained} avances ({action}); total acumulado {total}",
                              "Re-identified: +{gained} advances ({action}); running total {total}",
                              gained=found.start_advance, action=action, total=self._advance_offset),
                advance=self._advance_offset)
        self._emit(clock=new_rec.clock, state_hex=new_rec.result.state.to_hex32(), advance_offset=self._advance_offset,
                   message=t("Estado reidentificado", "State re-identified"),
                   seed_desc=t("Reidentificada ✓ ({used} parpadeos + {extra})", "Re-identified ✓ ({used} blinks + {extra})",
                               used=found.blinks_used, extra=self.cfg.reidentify_confirm_blinks))
        return "PLAN"

    def wait_trigger(self, ctx: StateContext) -> str:
        """WAIT_TRIGGER: espera la primera pulsación del plan vigilando la sincronía con cada parpadeo.

        Cada parpadeo coherente re-ancla el reloj y, si el plan se mueve más de 30 ms, se recalcula. Parpadeos
        incoherentes o perder el ojo devuelven a COLLECT. Antes de pulsar se exige escena y mando correctos.
        """
        plan: Plan = ctx.data["plan"]
        predictor: BlinkPredictor = ctx.data["predictor"]
        stream: BlinkEventStream = ctx.data["stream"]
        watcher: EyeWatcher = ctx.data["watcher"]
        trigger = plan.presses[0] if len(plan.presses) > 1 else plan.final
        rec: BlinkRecovery = ctx.data["recovery"]
        extra: list[tuple[int, float]] = []
        self._emit(phase="WAIT_TRIGGER", message=t("Esperando el momento exacto; vigilando la sincronía",
                                                   "Waiting for the exact moment; watching the sync"),
                   next_action=f"{trigger.trigger} ({trigger.label})", next_action_t=trigger.send_t)
        if self.cfg.mode is Mode.DRY_RUN:
            ctx.log("plan", t("ENSAYO: no se envían pulsaciones.\n", "DRY RUN: no presses are sent.\n")
                    + plan.describe(self._fmt_t))
            self._emit(phase="DONE", message=t("Ensayo completado: detección, estado y plan correctos (no se pulsó nada)",
                                               "Dry run complete: detection, state and plan are correct "
                                               "(nothing was pressed)"))
            return None
        recent: deque[bool] = deque(maxlen=self.cfg.desync_window)
        last_eye_t = ctx.now()
        last_blink_t = rec.clock.anchor_t
        stream.poll()  # descarta los ya usados para la recuperación
        stream.retention_s = 120.0  # en esperas de horas basta una ventana corta (umbral ya congelado)
        last_poll = ctx.now()
        while ctx.now() < trigger.send_t - self._approach_lead() - 0.3:
            sample = watcher.process(self._next_frame(ctx))
            watcher.samples.clear()
            stream.feed(sample)
            if sample.valid and sample.score >= self.cfg.scene_min_score:
                last_eye_t = ctx.now()
            elif ctx.now() - last_eye_t > self.cfg.scene_lost_s:
                ctx.log("error", t("No veo el ojo desde hace un rato (¿menú, pantalla apagada, otra escena?): "
                                   "se vuelve a observar antes de pulsar nada",
                                   "The eye has not been visible for a while (menu, screen off, another scene?): "
                                   "observing again before pressing anything"))
                return "COLLECT"
            self._keep_awake(ctx, next_press_t=trigger.send_t)
            if ctx.now() - last_poll < 0.5:
                continue
            last_poll = ctx.now()
            for e in stream.poll():
                if e.confidence < MIN_EVENT_CONFIDENCE:
                    continue
                ok = predictor.check(e.t_s, e.kind)
                recent.append(ok)
                if not ok:
                    ctx.log("warning", t("Parpadeo incoherente con el estado (t={ts:.2f})",
                                         "Blink inconsistent with the state (t={ts:.2f})", ts=e.t_s),
                            confidence=e.confidence)
                    if recent.count(False) >= 3 or list(recent)[-2:] == [False, False]:
                        ctx.log("error", t("Desincronización: se vuelve a recuperar el estado",
                                           "Desync: recovering the state again"))
                        return "COLLECT"
                    continue
                # Re-anclaje: el parpadeo confirma el estado y afina periodo/fase.
                last_blink_t = e.t_s
                extra.append((predictor.roll_at(e.t_s), e.t_s))
                clock = refine_clock(rec, extra)
                predictor.use_clock(clock)
                # Los relojes tienen anclas distintas: se compara el instante previsto de la misma cuenta.
                count = trigger.expected_count
                shift = clock.time_of_advance(count) - rec.clock.time_of_advance(count)
                ctx.log("reanchor", t("Parpadeo confirmado; periodo {period:.5f} s, desplazamiento del plan "
                                      "{shift:+.0f} ms",
                                      "Blink confirmed; period {period:.5f} s, plan shift {shift:+.0f} ms",
                                      period=clock.period_s, shift=shift * 1000), confidence=e.confidence)
                if abs(shift) > 0.03:
                    try:
                        plan = plan_static(rec.result.state, clock, self.d.encounter.blink_scene.scene,
                                           self.d.encounter.timeline, self.d.encounter.final, plan.target_advance,
                                           final_delay=plan.final_delay, now_s=ctx.now(),
                                           prep_s=0.3 + self._approach_lead(), lead_s=self.cfg.lead_s)
                    except PlanningError as exc:
                        # Cerca de la pulsación ya no se puede re-planificar con margen para la ruta, pero un
                        # desplazamiento pequeño (decenas de ms frente a ±0,5 s) se corrige moviendo la hora: mismo tick.
                        left = trigger.send_t + shift - ctx.now()
                        if abs(shift) < 0.5 * trigger.margin_s and left >= self._approach_lead():
                            plan = plan.shifted(shift)
                        else:
                            ctx.log("warning", t("El re-anclaje invalida el plan ({exc}); se busca otro objetivo",
                                                 "The re-anchor invalidates the plan ({exc}); looking for another "
                                                 "target", exc=exc))
                            return "PLAN"
                    rec = replace(rec, clock=clock)
                    ctx.data.update(plan=plan, recovery=rec)
                    trigger = plan.presses[0] if len(plan.presses) > 1 else plan.final
                    ctx.log("plan", t("Plan recalculado tras re-anclaje:\n", "Plan recalculated after re-anchor:\n")
                            + plan.describe(self._fmt_t))
                    self._emit(clock=clock, next_action_t=trigger.send_t)
        self._check_controller(t("antes de pulsar", "before pressing"))
        now = ctx.now()
        if now - last_eye_t > 2.0 or now - last_blink_t > self.cfg.max_blink_silence_s:
            raise InterventionRequired(t("La escena ya no es la esperada (no veo el ojo o no hay parpadeos recientes): "
                                         "no pulso para no estropear la partida",
                                         "The scene is no longer the expected one (eye not visible or no recent "
                                         "blinks): not pressing, to avoid ruining the game"))
        if self.d.encounter.approach:
            outcome = self._approach(ctx, trigger.send_t)
            if outcome is not None:
                return outcome
            plan = ctx.data["plan"]  # puede ser otro (reidentificación delante del legendario)
            trigger = plan.presses[0] if len(plan.presses) > 1 else plan.final
        ctx.sleep_until(trigger.send_t)
        late = ctx.now() - trigger.send_t
        if late > trigger.margin_s * 0.5:
            ctx.log("warning", t("La pulsación llegaría {late:.0f} ms tarde: se recalcula el plan",
                                 "The press would be {late:.0f} ms late: recalculating the plan", late=late * 1000))
            return "PLAN"
        self._press(ctx, Button.A if trigger.trigger.upper() == "A" else Button[trigger.trigger.upper()], trigger.label)
        return "TIMELINE" if len(plan.presses) > 1 else "ENCOUNTER"
