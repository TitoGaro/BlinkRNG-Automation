"""Regresiones de la revisión previa a las pruebas con hardware real."""
from pathlib import Path

import numpy as np
import pytest

from rngauto.app.session import HardwareSession, RoiTap
from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
from rngauto.control.controller import RecordingController
from rngauto.core.calibration import CalibrationHistory
from rngauto.core.clock import SimulatedClock
from rngauto.core.encounter import load_encounter
from rngauto.core.pokemon import TargetCriteria
from rngauto.core.statemachine import Outcome
from rngauto.games.bdsp.blink_pipeline import BlinkPredictor, recover_from_events
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.vision.blink_signal import MatchSample, extract_blinks
from rngauto.vision.blink_signal import BlinkEvent, EyeMatcher
from rngauto.vision.frames import Frame, NormBox

ROOT = Path(__file__).resolve().parents[2]


def sim_run(mode=Mode.AUTOMATIC, matcher=None, **cfg):
    enc = load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml")
    finder = StaticTargetFinder(static_template(enc.template))
    switch = SimulatedSwitch(finder, SimTruth(), seed=4)
    ctrl = switch.controller()
    ctrl.connect()
    matcher = matcher or EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height)
    deps = StaticDeps(clock=switch.clock, frames=switch, controller=ctrl, finder=finder, encounter=enc, matcher=matcher,
                      calibration=CalibrationHistory(enc.id, 84), in_battle=switch.in_battle, observe=switch.observe,
                      reset_game=switch.reset_game)
    run = StaticEncounterRun(deps, StaticRunConfig(criteria=TargetCriteria(natures=frozenset({3})), mode=mode,
                                                   lead_s=0.125, **cfg))
    return run, ctrl


@pytest.mark.slow
def test_dry_run_sends_absolutely_nothing():
    run, ctrl = sim_run(Mode.DRY_RUN, keep_awake_s=10.0)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS
    assert ctrl.log == []  # ni pulsaciones ni toques de stick antiatenuación


def test_no_eye_in_scene_stops_safely():
    noise = np.random.default_rng(0).integers(0, 255, (20, 30, 3), dtype=np.uint8)
    run, ctrl = sim_run(matcher=EyeMatcher(noise, NormBox(0.1, 0.1, 0.3, 0.3), reference_height=180),
                        scene_timeout_s=20.0)
    result = run.build().run()
    assert result.outcome is Outcome.INTERVENTION
    assert "ojo" in result.message
    assert not [c for _, c in ctrl.log if c.state.buttons]


def test_long_dips_are_not_blinks():
    samples = []
    for i in range(600):  # 10 s a 60 fps
        t = i / 60
        closed = (2.0 <= t < 2.1) or (5.0 <= t < 6.5)  # parpadeo real y oclusión de 1,5 s
        samples.append(MatchSample(t, 0.3 if closed else 0.95))
    blinks = extract_blinks(samples, threshold=0.3)
    assert len(blinks) == 1 and 2.0 <= blinks[0].t_s <= 2.1


def test_replacing_capture_keeps_esp32_open(tmp_path):
    from rngauto.gui.state import AppState

    state = AppState(tmp_path)
    ctrl = RecordingController()
    ctrl.connect()
    state.set_controller(ctrl, "esp32")

    class Dummy:
        closed = False

        def close(self):
            Dummy.closed = True

    s1 = HardwareSession(False, SimulatedClock(), frames=None, device=Dummy())
    state.set_session(s1)
    assert s1.controller is ctrl
    state.set_session(None)  # p. ej. «Buscar capturadoras» libera la captura
    assert Dummy.closed and ctrl.status().connected


def test_predictor_works_for_hours_long_waits():
    from rngauto.games.bdsp.scene import SceneModel, SceneSimulator
    from rngauto.games.bdsp.xorshift import XorshiftState

    sim = SceneSimulator.start(XorshiftState(1, 2, 3, 4), SceneModel(), schedule_pokemon_now=False)
    events = []
    while len(events) < 40:
        for ev in sim.step():
            if ev.player_blink is not None:
                events.append(BlinkEvent(ev.time_s, ev.player_blink, 1.0, 0.5, 4))
    rec = recover_from_events(events)
    predictor = BlinkPredictor(rec)
    later = []
    sim.run_until(sim.time_s + 3 * 3600)  # 3 horas después
    while len(later) < 5:
        for ev in sim.step():
            if ev.player_blink is not None:
                later.append(ev)
    assert all(predictor.check(ev.time_s, ev.player_blink) for ev in later)


def test_roi_tap_records_only_when_asked(tmp_path):
    class Source:
        i = 0

        def read(self):
            Source.i += 1
            return Frame(np.full((180, 320, 3), Source.i % 255, np.uint8), Source.i / 60, Source.i)

        def latest(self):
            return None

    tap = RoiTap(Source(), NormBox(0.4, 0.4, 0.1, 0.1), tmp_path)
    tap.read()
    assert not list(tmp_path.glob("*.avi"))
    tap.recording = True
    for _ in range(30):
        tap.read()
    tap.recording = False
    tap.read()
    tap.close()
    files = list(tmp_path.glob("ojo_*.avi"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    assert len((tmp_path / "ojo_01.frames.jsonl").read_text().splitlines()) == 30


def test_recovery_survives_a_double_seen_as_single():
    """Sin exclusiones, un doble leído como simple ya no impide recuperar la semilla (con ~40 parpadeos: con
    menos, sin la información de los «simples» el sistema queda indeterminado y se sigue observando)."""
    from rngauto.core.observation import BlinkKind
    from rngauto.games.bdsp.scene import SceneModel, SceneSimulator
    from rngauto.games.bdsp.xorshift import XorshiftState

    sim = SceneSimulator.start(XorshiftState(5, 6, 7, 8), SceneModel(), schedule_pokemon_now=False)
    events = []
    while len(events) < 40:
        for ev in sim.step():
            if ev.player_blink is not None:
                events.append(BlinkEvent(ev.time_s, ev.player_blink, 1.0, 0.5, 4))
    truth = recover_from_events(events, max_exclusions=0)
    i = next(k for k, e in enumerate(events) if e.kind is BlinkKind.DOUBLE)
    misread = [*events[:i], BlinkEvent(events[i].t_s, BlinkKind.SINGLE, 1.0, 0.5, 4), *events[i + 1:]]
    rec = recover_from_events(misread, max_exclusions=0)
    assert rec.result.state == truth.result.state
