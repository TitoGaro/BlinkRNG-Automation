"""El historial se ve aunque haya registros dañados o de otra versión."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rngauto.gui.pages.history import HistoryPage  # noqa: E402
from rngauto.gui.state import AppState  # noqa: E402


def test_damaged_records_are_skipped(tmp_path):
    app = QApplication.instance() or QApplication([])
    assert app is not None
    session = tmp_path / "sessions" / "20261015-200000-dialga"
    session.mkdir(parents=True)
    good = {"started_at": "2026-10-15T20:00:00", "encounter": "dialga", "target_advance": 1000, "resolved": 1002,
            "outcome": "miss", "observation": {"is_shiny": False}}
    lines = [json.dumps(good), "{ a medias", "[1, 2]", json.dumps({**good, "resolved": "mil"}),
             json.dumps({**good, "observation": "texto"})]
    (session / "attempts.jsonl").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / "dialga.json").write_text('{"encounter": 3}', encoding="utf-8")

    state = AppState(tmp_path)
    state.auto_hardware = False
    page = HistoryPage(state)
    page.on_enter()
    assert page.table.rowCount() == 2  # el bueno y el de la observación rara; el resto se omite
    assert page.table.item(1, 4).text() == "+2"
