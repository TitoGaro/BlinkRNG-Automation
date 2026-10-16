"""AdvancePlanner de BDSP: de "objetivo en el advance N" a "pulsa X en el instante t".

El TargetFinder dice *qué* advance; este módulo decide *cuándo* actuar:

1. Fase de espera (escena de parpadeos): el contador avanza en bloques de ``npc+1`` llamadas por tick
   (``AdvanceClock``). La pulsación que inicia el timeline se coloca en el **centro** de un periodo
   (margen ±P/2).
2. Timeline: tras la pulsación, la escena cambia y el RNG avanza según la simulación por eventos
   (lecturas en blanco, pausa, delay fijo, jugador + Pokémon NPC) — determinista dado el estado.
3. Pulsación final: debe caer cuando el contador vale ``objetivo − final_delay``; se coloca en el centro
   del hueco entre la llamada que lo alcanza y la siguiente. Si el hueco es estrecho, se prueba otro
   tick de inicio (cambia todo el timeline).

Todos los instantes están en tiempo de vídeo (el de los frames). ``lead_s`` adelanta el envío para
compensar la latencia vídeo + mando.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from rngauto.core.encounter import FinalAction, SceneSpec, TimelinePhase
from rngauto.core.tracker import AdvanceClock
from rngauto.i18n import t

from .scene import RngEvent, SceneModel, SceneSimulator
from .xorshift import Xorshift128, XorshiftState


class PlanningError(RuntimeError):
    """No hay plan que cumpla el objetivo con los márgenes exigidos; el mensaje dice el motivo."""

    pass


@dataclass(frozen=True, slots=True)
class PlannedPress:
    """Una pulsación planificada. Los márgenes son lo que puede adelantarse o retrasarse sin cambiar la cuenta."""

    label: str
    trigger: str
    video_t: float  # instante en que el juego debe recibirla (tiempo de vídeo)
    send_t: float  # instante de envío = video_t − lead
    expected_count: int  # llamadas consumidas cuando llega
    margin_before_s: float
    margin_after_s: float

    @property
    def margin_s(self) -> float:
        return min(self.margin_before_s, self.margin_after_s)


@dataclass(frozen=True)
class Plan:
    """Pulsaciones para un objetivo: la de inicio del timeline (si lo hay) y la final, en ese orden.

    ``trigger_tick`` es el tick del reloj periódico en que se inicia (``None`` si se planificó sobre una escena
    simulada) y ``timeline_events`` las llamadas previstas entre ambas pulsaciones.
    """

    target_advance: int
    final_delay: int
    presses: tuple[PlannedPress, ...]
    trigger_tick: int | None
    trigger_count_confidence: float
    timeline_events: tuple[RngEvent, ...] = field(repr=False)

    @property
    def final(self) -> PlannedPress:
        return self.presses[-1]

    def describe(self, fmt_t: Callable[[float], str] | None = None) -> str:
        """Resumen legible. ``fmt_t`` convierte los instantes a hora del reloj (p. ej. «20:41:03.25»)."""
        fmt = fmt_t or (lambda t: f"t={t:.3f}s")
        lines = [t("Objetivo {target} (delay final {delay})", "Target {target} (final delay {delay})",
                   target=self.target_advance, delay=self.final_delay)]
        for p in self.presses:
            lines.append(t(
                "  {label}: {trigger} a las {video} (se envía a las {send}), "
                "cuenta {count}, margen ±{margin_ms:.0f} ms",
                "  {label}: {trigger} at {video} (sent at {send}), "
                "count {count}, margin ±{margin_ms:.0f} ms",
                label=p.label, trigger=p.trigger, video=fmt(p.video_t), send=fmt(p.send_t), count=p.expected_count,
                margin_ms=p.margin_s * 1000,
            ))
        return "\n".join(lines)

    def shifted(self, dt_s: float) -> "Plan":
        """Mismo plan (mismos ticks) con las horas corregidas ``dt_s`` (re-anclaje del reloj)."""
        presses = tuple(replace(p, video_t=p.video_t + dt_s, send_t=p.send_t + dt_s) for p in self.presses)
        return replace(self, presses=presses)


def _scene_model(spec: SceneSpec, period_s: float) -> SceneModel:
    return SceneModel(
        npc=spec.npc, pokemon_npc=spec.pokemon_npc, player_rolls=spec.player_rolls,
        player_phase=spec.player_phase, period_s=period_s,
    )


def _stable_window(sim: SceneSimulator, count: int, horizon_s: float) -> tuple[float, float, list[RngEvent]] | None:
    """Intervalo [t_a, t_b) durante el que el contador vale exactamente ``count``.

    ``None`` si un bloque de llamadas simultáneas salta por encima de ``count`` o se supera el horizonte.
    """
    events: list[RngEvent] = []
    if sim.advance > count:
        return None
    t_a = sim.time_s
    while sim.advance < count:
        nxt = sim.peek_time()
        if nxt is None or nxt > horizon_s:
            return None
        batch = sim.step()
        events.extend(batch)
        t_a = batch[-1].time_s
    if sim.advance != count:
        return None
    t_b = sim.peek_time()
    if t_b is None:
        return None
    return t_a, t_b, events


def simulate_phase(base: XorshiftState, count_at_trigger: int, trigger_t: float, phase: TimelinePhase, period_s: float) -> SceneSimulator:
    """Simulador posicionado justo tras disparar ``phase`` en ``trigger_t``.

    Orden de la simulación: lecturas en blanco → pausa → delay fijo → programación del primer
    parpadeo de cada Pokémon NPC → eventos temporizados.
    """
    rng = Xorshift128.from_state(base)
    rng.jump(count_at_trigger)
    rng.advance(phase.blank_reads + phase.fixed_delay)
    return SceneSimulator.start(
        rng.state,
        _scene_model(phase.scene, period_s),
        start_time_s=trigger_t + phase.pause_s,
        start_advance=count_at_trigger + phase.blank_reads + phase.fixed_delay,
    )


def plan_static(
    base: XorshiftState,
    clock: AdvanceClock,
    idle_scene: SceneSpec,
    phases: tuple[TimelinePhase, ...],
    final: FinalAction,
    target_advance: int,
    *,
    final_delay: int | None = None,
    now_s: float,
    prep_s: float = 3.0,
    lead_s: float = 0.0,
    max_candidates: int = 200,
) -> Plan:
    """Calcula el plan de pulsaciones para que el Pokémon se genere en ``target_advance``.

    ``final_delay`` sustituye al de ``final`` (lo estima la calibración); ``prep_s`` es el tiempo mínimo desde
    ``now_s`` hasta la primera pulsación. Lanza ``PlanningError`` si ningún tick de inicio deja margen.
    """
    if idle_scene.pokemon_npc:
        raise PlanningError(t(
            "La escena de espera tiene Pokémon NPC: el reloj periódico no la modela "
            "(usar re-identificación con ruido; pendiente).",
            "The idle scene has NPC Pokémon: the periodic clock does not model it "
            "(noisy reidentification required; not implemented yet).",
        ))
    if len(phases) > 1:
        raise PlanningError(t("Solo se admite un timeline de una fase por ahora",
                              "Only single-phase timelines are supported for now"))
    delay = final.final_delay if final_delay is None else final_delay
    press_count = target_advance - delay
    stride = clock.calls_per_tick
    period = clock.period_s
    earliest = now_s + prep_s

    if not phases:
        # Pulsación final directamente en la escena de espera.
        if (press_count - clock.anchor_advance) % stride:
            raise PlanningError(t("El contador nunca vale exactamente objetivo−delay en esta escena (bloques de NPC)",
                                  "The counter never equals exactly target−delay in this scene (NPC blocks)"))
        tick = (press_count - clock.anchor_advance) // stride
        t_press = clock.time_of_tick(tick) + period / 2
        if t_press < earliest:
            raise PlanningError(t("Objetivo demasiado cercano: no da tiempo a pulsar",
                                  "Target too close: not enough time to press"))
        press = PlannedPress("final", final.trigger, t_press, t_press - lead_s, press_count, period / 2, period / 2)
        return Plan(target_advance, delay, (press,), tick, clock.count_confidence(t_press), ())

    phase = phases[0]
    # Llamadas que consume el inicio del timeline aunque no pase el tiempo: acotan el tick de inicio más tardío.
    fixed = phase.blank_reads + phase.fixed_delay + phase.scene.pokemon_npc
    k_max = (press_count - fixed - clock.anchor_advance) // stride
    best_failure = t("no hay ticks de inicio posibles", "no possible start ticks")
    checked = 0
    # Del tick más tardío hacia atrás: el primero viable da el timeline más corto.
    for k in range(k_max, -10**9, -1):
        t1 = clock.time_of_tick(k) + period / 2
        if t1 < earliest:
            best_failure = t("objetivo demasiado cercano para iniciar el timeline con la duración mínima",
                             "target too close to start the timeline with the minimum duration")
            break
        checked += 1
        if checked > max_candidates:
            best_failure = t("ningún tick de inicio de los {n} últimos da margen suficiente",
                             "none of the last {n} start ticks leaves enough margin", n=max_candidates)
            break
        c1 = clock.anchor_advance + k * stride
        sim = simulate_phase(base, c1, t1, phase, period)
        horizon = t1 + 3600.0  # tope de simulación: ningún timeline real dura una hora
        window = _stable_window(sim, press_count, horizon)
        if window is None:
            best_failure = t("el contador salta por encima del objetivo en bloque",
                             "the counter jumps past the target in a block")
            continue
        t_a, t_b, events = window
        t_a = max(t_a, t1 + phase.pause_s)  # el hueco no empieza antes de que acabe la pausa
        t2 = (t_a + t_b) / 2
        if t2 - t1 < final.min_timeline_s:
            best_failure = t("timeline más corto que la duración mínima", "timeline shorter than the minimum duration")
            continue
        margin = (t_b - t_a) / 2
        if margin < final.min_gap_margin_s:
            best_failure = t("hueco demasiado estrecho ({ms:.0f} ms)", "gap too narrow ({ms:.0f} ms)", ms=margin * 1000)
            continue
        trigger = PlannedPress(t("inicio timeline", "timeline start"), phase.trigger, t1, t1 - lead_s, c1, period / 2, period / 2)
        final_press = PlannedPress("final", final.trigger, t2, t2 - lead_s, press_count, margin, margin)
        return Plan(target_advance, delay, (trigger, final_press), k, clock.count_confidence(t1), tuple(events))
    raise PlanningError(t("No hay plan viable para el objetivo {target}: {reason}",
                          "No viable plan for target {target}: {reason}", target=target_advance, reason=best_failure))


def plan_static_in_scene(
    idle: SceneSimulator,
    horizon_s: float,
    phases: tuple[TimelinePhase, ...],
    final: FinalAction,
    target_advance: int,
    *,
    final_delay: int | None = None,
    now_s: float,
    prep_s: float = 3.0,
    lead_s: float = 0.0,
) -> Plan:
    """Como :func:`plan_static`, pero la espera es una escena simulada (jugador + Pokémon que parpadea) en vez de un
    reloj periódico: tras reidentificar delante del legendario. La pulsación de inicio va en el centro de un hueco
    entre llamadas y antes de ``horizon_s`` (hasta donde la escena predicha es fiable)."""
    if len(phases) > 1:
        raise PlanningError(t("Solo se admite un timeline de una fase", "Only single-phase timelines are supported"))
    delay = final.final_delay if final_delay is None else final_delay
    press_count = target_advance - delay
    earliest = now_s + prep_s
    sim = idle.fork()
    if not phases:
        # Una sola pulsación delante del legendario (Cresselia: se genera al hablarle): en el hueco en que el
        # contador vale exactamente objetivo − delay.
        while (nxt := sim.peek_time()) is not None and nxt <= horizon_s and sim.advance <= press_count:
            if sim.advance == press_count:
                t_press, gap = (sim.time_s + nxt) / 2, (nxt - sim.time_s) / 2
                if t_press >= earliest and gap >= final.min_gap_margin_s:
                    press = PlannedPress("final", final.trigger, t_press, t_press - lead_s, press_count, gap, gap)
                    return Plan(target_advance, delay, (press,), None, 1.0, ())
                break
            sim.step()
        raise PlanningError(t("No hay plan viable para el objetivo {target}: el contador no vale "
                              "{count} en un hueco a tiempo antes del horizonte fiable",
                              "No viable plan for target {target}: the counter does not reach "
                              "{count} in a gap in time before the reliable horizon",
                              target=target_advance, count=press_count))
    phase = phases[0]
    best: Plan | None = None
    best_failure = t("el objetivo ya ha pasado o no hay huecos antes del horizonte fiable",
                     "the target has already passed or there are no gaps before the reliable horizon")
    while (nxt := sim.peek_time()) is not None and nxt <= horizon_s and sim.advance < press_count:
        prev_t, c1 = sim.time_s, sim.advance
        t1 = (prev_t + nxt) / 2
        gap = (nxt - prev_t) / 2
        if t1 >= earliest and gap >= 0.25:  # la pulsación de inicio necesita al menos ±0,25 s de margen
            rng = Xorshift128.from_state(sim.state)
            rng.advance(phase.blank_reads + phase.fixed_delay)
            timeline = SceneSimulator.start(rng.state, _scene_model(phase.scene, idle.model.period_s),
                                            start_time_s=t1 + phase.pause_s,
                                            start_advance=c1 + phase.blank_reads + phase.fixed_delay)
            window = _stable_window(timeline, press_count, t1 + 3600.0)
            if window is None:
                best_failure = t("el contador salta por encima del objetivo en bloque",
                                 "the counter jumps past the target in a block")
            else:
                t_a, t_b, events = window
                t_a = max(t_a, t1 + phase.pause_s)
                t2 = (t_a + t_b) / 2
                margin = (t_b - t_a) / 2
                if t2 - t1 < final.min_timeline_s:
                    best_failure = t("timeline más corto que la duración mínima",
                                     "timeline shorter than the minimum duration")
                elif margin < final.min_gap_margin_s:
                    best_failure = t("hueco demasiado estrecho ({ms:.0f} ms)", "gap too narrow ({ms:.0f} ms)",
                                     ms=margin * 1000)
                else:
                    # Se queda con el último inicio viable: el timeline más corto (como ``plan_static``).
                    trigger = PlannedPress(t("inicio timeline", "timeline start"), phase.trigger, t1, t1 - lead_s, c1,
                                           gap, gap)
                    final_press = PlannedPress("final", final.trigger, t2, t2 - lead_s, press_count, margin, margin)
                    best = Plan(target_advance, delay, (trigger, final_press), None, 1.0, tuple(events))
        sim.step()
    if best is not None:
        return best
    raise PlanningError(t("No hay plan viable para el objetivo {target}: {reason}",
                          "No viable plan for target {target}: {reason}", target=target_advance, reason=best_failure))
