"""La rama «hardware real» de build_static_run (no demo), con la Switch simulada detrás.

Comprueba el cableado que solo se usa con hardware real: ojo desde la preparación guardada del usuario,
grabación del recorte del ojo (RoiTap), combate/verificación/reinicio a cargo del usuario.
"""
from pathlib import Path

import pytest

from rngauto.app.session import HardwareSession, RunRequest, build_static_run
from rngauto.app.setup_store import save_eye_setup
from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
from rngauto.core.encounter import load_encounter
from rngauto.core.pokemon import NATURES_ES, TargetCriteria
from rngauto.core.stats import compute_stats
from rngauto.core.statemachine import Outcome
from rngauto.core.timeline import Timeline
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.static_strategy import Mode

ROOT = Path(__file__).resolve().parents[2]


class LatestSwitch:
    """Adaptador: la Switch simulada con ``latest()`` como una capturadora."""

    def __init__(self, switch):
        self.switch = switch
        self._last = None

    def read(self):
        self._last = self.switch.read()
        return self._last

    def latest(self):
        return self._last


class User:
    def __init__(self, switch):
        self.switch = switch
        self.resets = 0

    def confirm(self, message, timeout_s=None):
        if "combate" in message:
            for _ in range(60):
                self.switch.read()
        return True

    def notify(self, message):
        pass

    def wait_ready(self, message):
        self.resets += 1
        self.switch.reset_game()
        return True

    def ask_observation(self, fields):
        from rngauto.app.console_operator import ConsoleOperator

        p = self.switch.game.generated
        tpl = self.switch.finder.template
        answers = iter(["s" if p.shiny else "n", NATURES_ES[p.nature],
                        " ".join(map(str, compute_stats(tpl.base_stats, p.ivs, tpl.level, p.nature)))])
        return ConsoleOperator(tpl, input_fn=lambda _p: next(answers), print_fn=lambda _s: None).ask_observation(fields)


@pytest.mark.slow
def test_real_branch_with_user_operator(tmp_path):
    enc = load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml")
    finder = StaticTargetFinder(static_template(enc.template))
    switch = SimulatedSwitch(finder, SimTruth(final_delay=86), seed=21)
    ctrl = switch.controller()
    ctrl.connect()
    # El usuario marcó el ojo en el paso «Escena»
    first = switch.read()
    box = switch.scene.eye_box
    save_eye_setup(tmp_path, enc.id, switch.renderer.template(), box, first.height)
    session = HardwareSession(False, switch.clock, LatestSwitch(switch), controller=ctrl)
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    user = User(switch)
    req = RunRequest(enc, TargetCriteria(natures=frozenset({15})), Mode.ASSISTED, 0.125, tmp_path, ROOT / "assets",
                     auto_battle=False)  # la Switch simulada no dibuja el combate: lo confirma el usuario
    run = build_static_run(session, req, user, Timeline(), None, session_dir)
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, result.message
    assert user.resets >= 1  # el primer intento falla (+2) y el usuario reinicia
    assert run.d.calibration.estimate_final_delay().value == 86
    assert list(session_dir.glob("ojo_*.avi"))  # grabación del ojo durante la recogida de parpadeos
    assert (session_dir / "attempts.jsonl").exists()
    assert (tmp_path / "calibration" / "dialga.json").exists()
