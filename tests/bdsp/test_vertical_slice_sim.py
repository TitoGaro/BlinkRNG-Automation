"""Vertical slice completo contra la Switch simulada.

Vídeo → parpadeos → estado → objetivo → plan → pulsaciones con latencia → encuentro → verificación →
calibración → reintento. El juego simulado usa un delay final real (86) distinto del prior (84) y
latencias de vídeo/mando que la estrategia no conoce.
"""
from pathlib import Path

import pytest

from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
from rngauto.core.calibration import CalibrationHistory
from rngauto.core.encounter import load_encounter
from rngauto.core.pokemon import TargetCriteria
from rngauto.core.statemachine import Outcome
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.vision.blink_signal import EyeMatcher

ROOT = Path(__file__).resolve().parents[2]


def build_run(truth: SimTruth, criteria: TargetCriteria, mode=Mode.AUTOMATIC, seed=11, calibration=None):
    enc = load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml")
    finder = StaticTargetFinder(static_template(enc.template))
    switch = SimulatedSwitch(finder, truth, seed=seed)
    controller = switch.controller()
    controller.connect()
    matcher = EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height)
    deps = StaticDeps(
        clock=switch.clock, frames=switch, controller=controller, finder=finder, encounter=enc,
        matcher=matcher, calibration=calibration or CalibrationHistory(enc.id, enc.final.final_delay),
        in_battle=switch.in_battle, observe=lambda fields: switch.observe(fields),
        reset_game=switch.reset_game,
    )
    cfg = StaticRunConfig(criteria=criteria, mode=mode, lead_s=0.125, max_attempts=4)
    return StaticEncounterRun(deps, cfg), switch


@pytest.mark.slow
def test_vertical_slice_calibrates_and_hits_target():
    criteria = TargetCriteria(natures=frozenset({15}))  # Modesta: muchos objetivos, test rápido
    run, switch = build_run(SimTruth(final_delay=86), criteria)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, (result.message, [e.message for e in run.d.timeline.of_kind("error", "warning")][-5:])
    # Primer intento: falla por +2 (delay real 86 vs prior 84); la calibración lo corrige
    assert run.attempts[0].delta == 2
    assert run.d.calibration.estimate_final_delay().value == 86
    assert run.attempts[-1].delta == 0
    # El Pokémon generado por el juego es exactamente el objetivo (advances relativos al estado
    # recuperado, que empieza en la tirada del primer parpadeo observado)
    expected = run.d.finder.generate(run.last_recovery.result.state, run.attempts[-1].target_advance)
    assert switch.game.generated == expected
    assert len(run.attempts) == 2
    assert run.d.timeline.of_kind("reanchor")  # la espera re-ancla con parpadeos nuevos


@pytest.mark.slow
def test_dry_run_stops_before_sending_inputs():
    run, switch = build_run(SimTruth(), TargetCriteria(natures=frozenset({3})), mode=Mode.DRY_RUN)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS
    assert not [c for _, c in run.d.controller.log if c.state.buttons]  # ninguna pulsación de botón
    assert run.d.timeline.of_kind("plan")
