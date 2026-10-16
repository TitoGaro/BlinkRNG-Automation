"""Flujo completo del usuario (Uxie): fuera de la cueva con el mensaje de Serbal, avance rápido, ruta y combate."""
from pathlib import Path

import pytest

from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
from rngauto.control.controller import Button
from rngauto.core.calibration import CalibrationHistory
from rngauto.core.encounter import load_encounter
from rngauto.core.pokemon import ShinyFilter, TargetCriteria
from rngauto.core.statemachine import Outcome
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.fast_advance import AdvanceStats, load_methods
from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.screen import ScreenSignatures

ROOT = Path(__file__).resolve().parents[2]
UXIE = ROOT / "encounters" / "bdsp" / "uxie.toml"


def build(truth: SimTruth, criteria: TargetCriteria, seed: int, direction: str = "LEFT", max_attempts: int = 3):
    enc = load_encounter(UXIE)
    finder = StaticTargetFinder(static_template(enc.template))
    switch = SimulatedSwitch(finder, truth, seed=seed)
    ctrl = switch.controller()
    ctrl.connect()
    matcher = EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height)
    deps = StaticDeps(
        clock=switch.clock, frames=switch, controller=ctrl, finder=finder, encounter=enc, matcher=matcher,
        calibration=CalibrationHistory(enc.id, enc.final.final_delay), in_battle=switch.in_battle,
        observe=lambda fields: switch.observe(fields), reset_game=switch.reset_game,
        fast_methods=tuple(load_methods().values()), fast_stats=AdvanceStats(), screens=ScreenSignatures(None),
    )
    cfg = StaticRunConfig(criteria=criteria, mode=Mode.AUTOMATIC, lead_s=0.125, max_attempts=max_attempts,
                          min_plateau_blinks=12, blink_threshold_window_s=60, registered_item_direction=direction)
    return StaticEncounterRun(deps, cfg), switch


def _msgs(run, *kinds):
    return [e.message for e in run.d.timeline.of_kind(*kinds)]


def test_uxie_config_is_valid():
    enc = load_encounter(UXIE)
    assert enc.blink_scene.message and enc.approach and enc.approach_lead_s == 14.0
    assert enc.final.final_delay == 85


@pytest.mark.slow
def test_full_flow_outside_the_cave():
    truth = SimTruth(message_scene=True, item_direction="LEFT", final_delay=86)  # prior 85: se calibra +1
    run, switch = build(truth, TargetCriteria(shiny=ShinyFilter.SHINY), seed=6)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, (result.message, _msgs(run, "error", "warning")[-6:])
    assert switch.game.generated.shiny.value != 0
    assert not switch.player_moved and switch.wrong_talk == 0  # solo se habló estando delante de Uxie
    log = " | ".join(_msgs(run, "input", "detection"))
    assert "Aproximación" in log and "Diálogo con el legendario" in log
    # se usó la caña («+» e izquierda con la cruceta) para volver a poner el mensaje tras el avance rápido
    assert any(c.state.dpad.name == "LEFT" and "caña" in c.label for _, c in run.d.controller.log)


@pytest.mark.slow
def test_after_a_miss_the_game_is_reset_and_the_message_comes_back():
    truth = SimTruth(message_scene=True, item_direction="UP", final_delay=88)  # prior 85: 1.er intento falla
    run, switch = build(truth, TargetCriteria(natures=frozenset({15})), seed=11, direction="UP", max_attempts=4)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, (result.message, _msgs(run, "error", "warning")[-6:])
    assert switch.resets >= 1 and len(run.attempts) >= 2
    assert run.d.calibration.estimate_final_delay().value == 88
    ups = [c for _, c in run.d.controller.log if c.state.dpad.name == "UP" and "caña" in c.label]
    assert len(ups) >= switch.resets  # caña al empezar tras cada reinicio (el juego carga sin el mensaje)


@pytest.mark.slow
def test_wrong_item_direction_stops_safely():
    truth = SimTruth(message_scene=True, item_direction="RIGHT")
    run, switch = build(truth, TargetCriteria(natures=frozenset({15})), seed=11, direction="LEFT")
    switch.game.message = False  # p. ej. tras un reinicio: el programa debe poner el mensaje
    result = run.build().run()
    assert result.outcome is Outcome.INTERVENTION and "caña" in result.message
    assert not [c for _, c in run.d.controller.log if c.state.buttons & Button.A]  # no habló con nada
