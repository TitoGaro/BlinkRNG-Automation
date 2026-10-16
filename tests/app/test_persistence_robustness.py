"""Ficheros del usuario dañados, rutas con caracteres no latinos y registros cerrados: nada impide arrancar."""
import json

import numpy as np
import pytest

from rngauto import i18n
from rngauto.app.setup_store import Preferences, load_eye_setup, save_eye_setup
from rngauto.core.calibration import MAX_UNCERTAIN, AttemptRecord, CalibrationHistory
from rngauto.core.timeline import JsonlSink, TimelineEvent
from rngauto.vision.frames import NormBox
from rngauto.vision.imageio import imread, imwrite


@pytest.fixture
def unicode_dir(tmp_path):
    folder = tmp_path / "José_山田"  # como la carpeta de usuario de muchos jugadores
    folder.mkdir()
    return folder


def test_images_with_a_non_latin_path(unicode_dir):
    img = np.arange(30 * 20 * 3, dtype=np.uint8).reshape(20, 30, 3)
    path = unicode_dir / "ojo.png"
    assert imwrite(path, img)
    assert np.array_equal(imread(path), img)
    assert imread(unicode_dir / "no_existe.png") is None


def test_eye_setup_round_trip_and_damaged_file(unicode_dir):
    tpl = np.full((12, 16, 3), 200, np.uint8)
    save_eye_setup(unicode_dir, "dialga", tpl, NormBox(0.4, 0.4, 0.05, 0.05), 1080)
    setup = load_eye_setup(unicode_dir, "dialga")
    assert setup is not None and setup.load_template().shape == tpl.shape
    (unicode_dir / "setups" / "dialga.json").write_text("{ medio fichero", encoding="utf-8")
    assert load_eye_setup(unicode_dir, "dialga") is None  # se vuelve a marcar el ojo, sin excepción


@pytest.mark.parametrize("content", ["no es json", "[1, 2, 3]", '{"lead_s": "rápido", "tid": []}'])
def test_damaged_preferences_fall_back_to_defaults(tmp_path, content):
    (tmp_path / "preferences.json").write_text(content, encoding="utf-8")
    prefs = Preferences.load(tmp_path)
    prefs.save(tmp_path)  # y se pueden volver a guardar
    assert json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))


def test_damaged_calibration_is_set_aside(tmp_path):
    path = tmp_path / "calibration" / "dialga.json"
    path.parent.mkdir()
    path.write_text('{"encounter": "dialga", "confirmed": [', encoding="utf-8")
    history = CalibrationHistory.load(path, "dialga", 84)
    assert history.estimate_final_delay().value == 84 and not history.confirmed
    assert path.with_suffix(".json.bad").exists() and not path.exists()


def test_uncertain_attempts_are_capped():
    history = CalibrationHistory("dialga", 84)
    for i in range(MAX_UNCERTAIN + 15):
        # Siempre las mismas dos lecturas posibles, muy separadas: la votación empata y nunca se resuelve.
        history.record(AttemptRecord(attempt_id=str(i), encounter="dialga", config_version="1", engine="test",
                                     engine_version="1", started_at="2026-10-15T20:00:00", target_advance=1000,
                                     final_delay_used=84, candidates=[1000, 1500]))
    assert len(history.uncertain) == MAX_UNCERTAIN
    assert history.uncertain[-1].attempt_id == str(MAX_UNCERTAIN + 14)  # se quedan los más recientes


def test_sink_ignores_events_after_close(tmp_path):
    sink = JsonlSink(tmp_path / "timeline.jsonl")
    sink(TimelineEvent(1.0, "input", "A"))
    sink.close()
    sink(TimelineEvent(2.0, "input", "B"))  # una ejecución abandonada que aún emite
    sink.close()
    assert (tmp_path / "timeline.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_thousands_separator_follows_the_language():
    i18n.set_language("es")
    assert i18n.fmt_int(1234567) == "1.234.567"
    i18n.set_language("en")
    assert i18n.fmt_int(1234567.4) == "1,234,567"
