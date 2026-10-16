"""Planificador e identificación: el plan, ejecutado contra el simulador, cae en el objetivo."""
from pathlib import Path

import pytest

from rngauto.core.encounter import FinalAction, SceneSpec, TimelinePhase, load_encounter, load_encounters
from rngauto.core.pokemon import ShinyFilter, TargetCriteria
from rngauto.core.tracker import AdvanceClock
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.identify import ambiguity, candidate_advances, observe_like
from rngauto.games.bdsp.planner import PlanningError, plan_static, simulate_phase
from rngauto.games.bdsp.xorshift import XorshiftState

ROOT = Path(__file__).resolve().parents[2]
BASE = XorshiftState.from_hex64(0x1234567887654321, 0x8765432112345678)
PHASE = TimelinePhase("entrada", "A", blank_reads=2, pause_s=2.0, fixed_delay=0, scene=SceneSpec(pokemon_npc=1))
FINAL = FinalAction("A", final_delay=84, min_timeline_s=30.0, min_gap_margin_s=0.12)


def execute(plan, base, clock, phase):
    """Simula lo que haría el juego si recibe las pulsaciones del plan en sus instantes."""
    trigger, final = plan.presses
    count_at_trigger = clock.advance_at(trigger.video_t)
    sim = simulate_phase(base, count_at_trigger, trigger.video_t, phase, clock.period_s)
    sim.run_until(final.video_t)
    return sim.advance + plan.final_delay  # el juego genera tras ``final_delay`` llamadas


@pytest.mark.parametrize("target", [1595, 2400, 5000])
def test_plan_hits_target_in_simulation(target):
    clock = AdvanceClock(anchor_t=50.0, anchor_advance=300, period_s=1.0197)
    plan = plan_static(BASE, clock, SceneSpec(), (PHASE,), FINAL, target, now_s=60.0)
    assert plan.final.margin_s >= FINAL.min_gap_margin_s
    assert plan.final.video_t - plan.presses[0].video_t >= FINAL.min_timeline_s
    assert execute(plan, BASE, clock, PHASE) == target
    # Con un error de latencia menor que el margen se sigue acertando
    from dataclasses import replace
    late = replace(plan, presses=(plan.presses[0], replace(plan.final, video_t=plan.final.video_t + plan.final.margin_s * 0.8)))
    assert execute(late, BASE, clock, PHASE) == target


def test_plan_rejects_target_too_close():
    clock = AdvanceClock(0.0, 1000, 1.0197)
    with pytest.raises(PlanningError):
        plan_static(BASE, clock, SceneSpec(), (PHASE,), FINAL, 1100, now_s=1.0)


def test_plan_without_timeline_uses_idle_clock():
    clock = AdvanceClock(0.0, 0, 1.0, calls_per_tick=1)
    plan = plan_static(BASE, clock, SceneSpec(), (), FinalAction("A", final_delay=0), 500, now_s=1.0)
    assert len(plan.presses) == 1
    assert clock.advance_at(plan.final.video_t) == 500


def test_lead_shifts_send_time():
    clock = AdvanceClock(50.0, 300, 1.0197)
    plan = plan_static(BASE, clock, SceneSpec(), (PHASE,), FINAL, 2400, now_s=60.0, lead_s=0.15)
    for p in plan.presses:
        assert p.send_t == pytest.approx(p.video_t - 0.15)


def test_identify_hit_from_observation():
    finder = StaticTargetFinder(static_template("dialga"), 12345, 54321)
    target = 1595
    hit = finder.generate(BASE, target + 2)
    obs = observe_like(hit, ["nature", "ivs"])
    cands = candidate_advances(finder, BASE, target, obs, radius=20)
    assert target + 2 in cands
    # Todos los candidatos son indistinguibles con lo observado
    assert all(obs.compatible(finder.generate(BASE, c)) for c in cands)
    # Fenómeno real de BDSP: advances consecutivos pueden dar IVs/naturaleza/habilidad idénticas
    # (las repeticiones del sorteo de IVs a 31 "absorben" el desplazamiento). 1594-1596 coinciden.
    same = [finder.generate(BASE, a) for a in (1594, 1595, 1596)]
    assert len({(p.ivs, p.nature, p.ability_slot) for p in same}) == 1
    assert len({p.ec for p in same}) == 3
    assert ambiguity(finder, BASE, target, ["nature", "ivs"], radius=5) == 3
    assert ambiguity(finder, BASE, target, ["nature"], radius=5) >= 3


def test_end_to_end_target_then_plan():
    finder = StaticTargetFinder(static_template("dialga"))
    targets = finder.search(BASE, TargetCriteria(shiny=ShinyFilter.SHINY), 1000, 3000)
    assert targets
    clock = AdvanceClock(0.0, 0, 1.0197)
    plan = plan_static(BASE, clock, SceneSpec(), (PHASE,), FINAL, targets[0].advance, now_s=5.0)
    assert execute(plan, BASE, clock, PHASE) == targets[0].advance


def test_encounter_configs_load():
    configs = load_encounters(ROOT / "encounters" / "bdsp")
    dialga = configs["dialga"]
    assert dialga.template == "dialga" and dialga.final.final_delay == 84
    assert dialga.timeline[0].scene.pokemon_npc == 1
    assert static_template(dialga.template).species == 483
    assert load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml").label == "dialga-v0.1"


def test_shifted_plan_keeps_ticks_and_moves_times():
    """Re-anclaje cerca de la pulsación: mismo tick, solo se corrige la hora (no se abandona el objetivo)."""
    from rngauto.games.bdsp.planner import Plan, PlannedPress

    p = Plan(500, 85, (PlannedPress("inicio timeline", "A", 100.0, 99.76, 400, 0.5, 0.5),
                       PlannedPress("final", "A", 116.0, 115.76, 415, 0.5, 0.5)), 7, 1.0, ())
    s = p.shifted(-0.04)
    assert [x.expected_count for x in s.presses] == [400, 415]
    assert abs(s.presses[0].send_t - 99.72) < 1e-9 and abs(s.presses[1].video_t - 115.96) < 1e-9
    assert "a las 12:00:00.00" in p.describe(lambda t: "12:00:00.00")
