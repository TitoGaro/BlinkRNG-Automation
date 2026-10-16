"""VERIFY nunca reinicia el juego si lo obtenido puede ser el shiny, pero tampoco se para por nada.

Los advances vecinos dan la misma ficha (naturaleza e IVs) y solo cambia el brillo, así que la ficha sola no
distingue un fallo por uno del shiny buscado: decide el veredicto del combate (o la respuesta del usuario)."""
import itertools
from types import SimpleNamespace

import pytest

from rngauto.core.calibration import CalibrationHistory
from rngauto.core.encounter import load_encounters
from rngauto.core.pokemon import IVRange, Shiny, ShinyFilter, TargetCriteria
from rngauto.core.statemachine import InterventionRequired
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.identify import Observation
from rngauto.games.bdsp.static_strategy import Mode, StaticEncounterRun
from rngauto.games.bdsp.xorshift import XorshiftState
from rngauto.gui.state import ENCOUNTERS_DIR

BASE = XorshiftState.from_hex64(0x1234567887654321, 0x8765432112345678)


def _verify(obs: Observation, target, seen_not_shiny: bool = False):
    enc = load_encounters(ENCOUNTERS_DIR)["dialga"]
    run = object.__new__(StaticEncounterRun)
    run.d = SimpleNamespace(encounter=enc, finder=StaticTargetFinder(static_template("dialga")),
                            observe=lambda fields: obs, operator=None, calibration=CalibrationHistory("dialga", 84),
                            on_attempt=None)
    run.cfg = SimpleNamespace(observe_fields=("is_shiny", "nature", "ivs"), identify_radius=40,
                              criteria=TargetCriteria(shiny=ShinyFilter.SHINY), lead_s=0.1, mode=Mode.AUTOMATIC)
    run._emit = lambda **k: None
    run._ids = itertools.count(1)
    run._early_starts = 0
    run.attempts = []
    plan = SimpleNamespace(final=SimpleNamespace(margin_s=0.3), presses=())
    ctx = SimpleNamespace(data={"recovery": SimpleNamespace(result=SimpleNamespace(state=BASE)), "target": target,
                                "plan": plan, "delay": 84, "seen_not_shiny": seen_not_shiny},
                          log=lambda *a, **k: None, check=lambda: None)
    return run.verify(ctx)


@pytest.fixture(scope="module")
def shiny_target():
    finder = StaticTargetFinder(static_template("dialga"))
    target = finder.search(BASE, TargetCriteria(shiny=ShinyFilter.SHINY), 1000, 50_000, limit=1)[0]
    assert target.traits.shiny is not Shiny.NONE
    return target


def _same_summary_as(target) -> Observation:
    """Lo que da la ficha (sin brillo) del shiny buscado, o de un vecino: es la misma."""
    p = target.traits
    return Observation(nature=p.nature, ivs=tuple(IVRange(v, v) for v in p.ivs))


def test_without_a_clear_battle_verdict_it_stops(shiny_target):
    with pytest.raises(InterventionRequired) as info:
        _verify(_same_summary_as(shiny_target), shiny_target)
    assert not info.value.recoverable


def test_a_clear_not_shiny_battle_is_not_a_false_alarm(shiny_target):
    """Fallo por uno con el combate claramente normal: se reinicia (antes se paraba en casi cada intento)."""
    assert _verify(_same_summary_as(shiny_target), shiny_target, seen_not_shiny=True) == "RESET"
