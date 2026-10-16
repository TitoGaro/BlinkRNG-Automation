"""Nada se queda colgado: diálogos que se cierran solos, operaciones en segundo plano con tiempo máximo."""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from rngauto.games.bdsp.encounters import static_template  # noqa: E402
from rngauto.gui.operator import GuiOperator  # noqa: E402
from rngauto.gui.widgets import Background  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, predicate, timeout_s=5.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


_PARENTS = []  # en la aplicación el padre es la ventana principal, que vive siempre


def _parent():
    _PARENTS.append(QWidget())
    return _PARENTS[-1]


def _in_thread(fn):
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    t.start()
    return t, box


def test_confirm_times_out_and_closes_its_dialog(app):
    parent = _parent()
    op = GuiOperator(parent, [static_template("dialga")])
    t, box = _in_thread(lambda: op.confirm("¿Ejecutar este plan?", timeout_s=0.5))
    assert pump(app, lambda: bool(op._open))  # el diálogo se muestra (no modal)
    assert pump(app, lambda: not t.is_alive() and not op._open)
    assert box["value"] is None


def test_stop_closes_pending_dialogs_and_releases_worker(app):
    parent = _parent()
    op = GuiOperator(parent, [static_template("dialga")])
    t, box = _in_thread(lambda: op.wait_ready("Reinicia el juego"))
    assert pump(app, lambda: bool(op._open))
    op.cancel_pending()
    assert pump(app, lambda: not t.is_alive() and not op._open)


def test_ready_dialog_offers_stop(app):
    parent = _parent()
    op = GuiOperator(parent, [static_template("dialga")])
    stops = []
    op.stop_requested.connect(lambda: stops.append(True))
    t, _box = _in_thread(lambda: op.wait_ready("Reinicia el juego"))
    assert pump(app, lambda: bool(op._open))
    dlg = next(iter(op._open.values()))
    stop_btn = next(b for b in dlg.buttons() if b.text() == "Detener")
    stop_btn.click()
    assert pump(app, lambda: not t.is_alive() and stops)


def test_background_timeout_frees_the_page_and_late_result_is_released(app):
    bg = Background()
    failed, done, late = [], [], []
    bg.failed.connect(failed.append)
    bg.done.connect(done.append)
    bg.late.connect(late.append)
    release = threading.Event()
    bg.run(lambda: release.wait(5) and "puerto", timeout_s=0.2, timeout_message="no responde")
    assert pump(app, lambda: failed == ["no responde"])
    assert not bg.running
    release.set()
    assert pump(app, lambda: late == ["puerto"])
    assert done == []


def test_background_reports_any_exception(app):
    bg = Background()
    failed = []
    bg.failed.connect(failed.append)

    def boom():
        raise KeyboardInterrupt  # incluso BaseException: la página siempre recibe respuesta

    bg.run(boom)
    assert pump(app, lambda: len(failed) == 1)


def test_closing_a_question_is_not_a_no(app):
    """Cerrar «¿ha salido shiny?» con la X o Esc no cuenta como «No» (abriría la ficha con un shiny en la mano)."""
    parent = _parent()
    op = GuiOperator(parent, [static_template("dialga")])
    result = {}
    th = threading.Thread(target=lambda: result.update(v=op.confirm("¿Ha salido SHINY?")), daemon=True)
    th.start()
    assert pump(app, lambda: bool(op._open))
    next(iter(op._open.values())).reject()  # como la X / Esc
    assert pump(app, lambda: not th.is_alive())
    assert result["v"] is None
