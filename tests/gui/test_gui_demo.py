"""La interfaz guiada, de principio a fin, en modo demo (sin pantalla: QT_QPA_PLATFORM=offscreen)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rngauto.app.session import HardwareSession  # noqa: E402
from rngauto.games.bdsp.static_strategy import Mode  # noqa: E402
from rngauto.gui import theme  # noqa: E402
from rngauto.gui.main import MainWindow  # noqa: E402
from rngauto.gui.state import AppState  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(theme.STYLE)
    return a


def wait_until(app, predicate, timeout_s):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_wizard_guides_and_blocks_until_ready(app, tmp_path):
    st = AppState(tmp_path)
    st.auto_hardware = False  # nunca tocar el hardware real en tests
    w = MainWindow(st)
    w.show()
    app.processEvents()
    assert w.stack.currentIndex() == 0 and w.next_btn.isEnabled()  # hay Pokémon preseleccionado
    w.go(1)
    assert w.stack.currentIndex() == 1
    assert not w.next_btn.isEnabled() and "capturadora" in w.hint.text().lower()
    w.go(4)  # no se puede saltar a Ejecutar sin hardware
    assert w.stack.currentIndex() == 1
    w.close()


@pytest.mark.slow
def test_full_demo_run_from_gui(app, tmp_path):
    state = AppState(tmp_path)
    state.auto_hardware = False
    w = MainWindow(state)
    w.show()
    objective = w.pages[0]
    for i in range(objective.list.count()):  # la Switch simulada reproduce la escena de Dialga
        if objective.list.item(i).data(Qt.ItemDataRole.UserRole) == "dialga":
            objective.list.setCurrentRow(i)
    assert state.selection.encounter.id == "dialga"
    objective.shiny.setChecked(False)
    objective.nature.setCurrentIndex(objective.nature.findText("Modesta"))
    for b in objective.mode_group.buttons():
        if b.property("mode") == Mode.AUTOMATIC.value:
            b.click()
    app.processEvents()
    assert state.selection.mode is Mode.AUTOMATIC and state.selection.criteria.natures == frozenset({15})

    w.go(1)
    state.selection.demo = True
    state.set_session(HardwareSession.demo_session(state.selection.encounter, speed=400))
    w.pages[1].completion_changed.emit()
    assert w.next_btn.isEnabled()
    for step in (2, 3, 4):
        w.go(step)
        assert w.stack.currentIndex() == step, w.hint.text()
        app.processEvents()

    closer = QTimer()  # cierra los avisos modales de fin (el test no tiene usuario)
    closer.timeout.connect(lambda: app.activeModalWidget() and app.activeModalWidget().close())
    closer.start(200)
    run = w.run_page
    run.start_btn.click()
    assert state.running and w.emergency.isVisible()
    assert wait_until(app, lambda: not state.running, timeout_s=240), run.message.text()
    closer.stop()
    assert "Objetivo conseguido" in run.message.text(), (run.message.text(), run.log.toPlainText()[-2000:])
    assert (tmp_path / "calibration" / "dialga-demo.json").exists()
    w.go(5)
    assert w.pages[5].table.rowCount() >= 1
    w.close()
