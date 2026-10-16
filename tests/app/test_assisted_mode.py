"""Modo asistido (operador de consola) y medición de latencia, sin hardware."""
from pathlib import Path

import numpy as np
import pytest

from rngauto.app.console_operator import ConsoleOperator
from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
from rngauto.control.controller import RecordingController
from rngauto.core.calibration import CalibrationHistory
from rngauto.core.clock import SimulatedClock
from rngauto.core.encounter import load_encounter
from rngauto.core.pokemon import NATURES_ES, TargetCriteria
from rngauto.core.stats import compute_stats
from rngauto.core.statemachine import Outcome
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.frames import Frame

ROOT = Path(__file__).resolve().parents[2]


class ScriptedUser:
    """Responde como lo haría el usuario mirando la Switch simulada."""

    def __init__(self, switch: SimulatedSwitch) -> None:
        self.switch = switch
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "combate" in prompt:
            for _ in range(60):  # el usuario mira la pantalla ~2 s antes de contestar
                self.switch.read()
        p = self.switch.game.generated
        if "shiny" in prompt:
            return "s" if p.shiny else "n"
        if "Naturaleza" in prompt:
            return NATURES_ES[p.nature]
        if "Stats" in prompt:
            tpl = self.switch.finder.template
            return " ".join(str(v) for v in compute_stats(tpl.base_stats, p.ivs, tpl.level, p.nature))
        return "s"  # confirmaciones (pulsar, combate) e Intro


@pytest.mark.slow
def test_assisted_mode_with_typed_stats():
    enc = load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml")
    tpl = static_template(enc.template)
    finder = StaticTargetFinder(tpl)
    switch = SimulatedSwitch(finder, SimTruth(final_delay=85), seed=5)
    user = ScriptedUser(switch)
    controller = switch.controller()
    controller.connect()
    deps = StaticDeps(
        clock=switch.clock, frames=switch, controller=controller, finder=finder, encounter=enc,
        matcher=EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height),
        calibration=CalibrationHistory(enc.id, enc.final.final_delay),
        in_battle=None, observe=lambda fields: None, reset_game=switch.reset_game,
        operator=ConsoleOperator(tpl, input_fn=user, print_fn=lambda s: None),
    )
    run = StaticEncounterRun(deps, StaticRunConfig(criteria=TargetCriteria(natures=frozenset({15})),
                                                   mode=Mode.ASSISTED, lead_s=0.125, max_attempts=4))
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, result.message
    # Delay real 85 vs prior 84: el primer intento falla por +1 (o queda ambiguo entre vecinos idénticos)
    assert run.attempts[0].delta in (1, None)
    assert 1 in [c - run.attempts[0].target_advance for c in run.attempts[0].candidates]
    assert run.attempts[-1].target_advance in run.attempts[-1].candidates
    assert any("Ejecutar este plan" in p for p in user.prompts)
    assert any("Stats" in p for p in user.prompts)


def test_console_operator_rejects_impossible_stats():
    tpl = static_template("dialga")
    answers = iter(["n", "Modesta", "1 1 1 1 1 1", ""])
    out = []
    op = ConsoleOperator(tpl, input_fn=lambda p: next(answers), print_fn=out.append)
    obs = op.ask_observation(["nature", "ivs"])
    assert obs.nature == 15 and obs.is_shiny is False and obs.ivs is None
    assert any("imposible" in line for line in out)


class _FakeScreen:
    """Pantalla que cambia (abre "menú") 120 ms después de recibir la pulsación."""

    def __init__(self, clock: SimulatedClock, controller: RecordingController) -> None:
        self.clock, self.controller = clock, controller
        self.i = 0

    def read(self):
        t = self.clock.now()
        self.clock.advance(1 / 60)
        presses = [c.submitted_at for c in self.controller.log if c.command.label == "latencia"]
        undos = [c.submitted_at for c in self.controller.log if c.command.label == "deshacer"]
        menu_open = presses and t >= presses[-1] + 0.12 and not (undos and undos[-1] >= presses[-1])
        img = np.full((90, 160, 3), 200 if menu_open else 40, np.uint8)
        self.i += 1
        return Frame(img, t, self.i)
