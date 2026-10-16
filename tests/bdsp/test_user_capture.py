"""Cresselia: el programa le habla, el usuario la captura y pulsa «Listo». Si no es shiny, el programa lee su
ficha; si lo es (o no contesta), se para sin tocar el mando: abrir la ficha arriesgaría el shiny."""
from types import SimpleNamespace

import pytest

import rngauto.games.bdsp.static_run.capture as capture
from rngauto.core.encounter import load_encounters
from rngauto.core.statemachine import InterventionRequired
from rngauto.games.bdsp.static_strategy import Mode, StaticEncounterRun
from rngauto.gui.state import ENCOUNTERS_DIR


def _run(monkeypatch, shiny_answer):
    enc = load_encounters(ENCOUNTERS_DIR)["cresselia"]
    assert enc.capture == "user" and not enc.timeline and enc.extra_eyes == ("cresselia_delante",)
    calls = []
    run = object.__new__(StaticEncounterRun)
    run.d = SimpleNamespace(encounter=enc, finder=SimpleNamespace(template=SimpleNamespace(name="Cresselia")),
                            operator=SimpleNamespace(wait_ready=lambda msg: calls.append(("listo", msg)) or True,
                                                     confirm=lambda msg, timeout_s=None: calls.append(("shiny?", msg))
                                                     or shiny_answer))
    run.cfg = SimpleNamespace(mode=Mode.AUTOMATIC, party_slot=6)
    run._emit = lambda **k: calls.append(("fase", k.get("phase")))
    run._record_shiny = lambda ctx: calls.append(("calibrar", None))
    monkeypatch.setattr(capture, "open_menu_from_anywhere", lambda io: calls.append(("menú", None)))
    monkeypatch.setattr(capture, "open_stats_page", lambda io, slot: calls.append(("ficha", slot)) or ("img", "notas"))
    ctx = SimpleNamespace(data={}, log=lambda *a, **k: None, check=lambda: None, now=lambda: 0.0)
    return run, ctx, calls


def test_not_shiny_reads_the_summary(monkeypatch):
    run, ctx, calls = _run(monkeypatch, False)
    assert run.encounter(ctx) == "VERIFY"
    # tras «Listo» y «no es shiny», el programa abre él el menú (esté donde esté el usuario) y va a la ficha del 6.º
    steps = [c[0] for c in calls if c[0] != "fase"]
    assert steps == ["listo", "shiny?", "menú", "ficha"] and ("ficha", 6) in calls
    assert run._battle_possible  # nunca reinicia solo con el Pokémon en la mano
    assert ctx.data["summary_image"] == "img"


def test_shiny_stops_without_touching_anything(monkeypatch):
    run, ctx, calls = _run(monkeypatch, True)
    assert run.encounter(ctx) is None
    steps = [c[0] for c in calls]
    assert "menú" not in steps and "ficha" not in steps and "calibrar" in steps and ("fase", "SHINY") in calls


def test_no_answer_never_risks_the_shiny(monkeypatch):
    run, ctx, calls = _run(monkeypatch, None)
    with pytest.raises(InterventionRequired):
        run.encounter(ctx)
    assert "menú" not in [c[0] for c in calls]
