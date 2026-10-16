"""Encuentros con reidentificación delante del legendario (Regigigas): la «Escena» pide dos ojos."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from rngauto.app.setup_store import load_eye_setup, save_eye_setup  # noqa: E402
from rngauto.gui.pages.scene import ScenePage  # noqa: E402
from rngauto.gui.state import AppState  # noqa: E402
from rngauto.vision.frames import NormBox  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def eye():
    img = np.zeros((20, 14, 3), np.uint8)
    img[5:15, 4:10] = 200
    return img


def test_regigigas_needs_both_eyes(app, tmp_path):
    st = AppState(tmp_path)
    st.auto_hardware = False
    st.selection.encounter = st.encounters["regigigas"]
    st.set_item_direction("LEFT")
    page = ScenePage(st)
    page.on_enter()
    assert page.eye_choice.count() == 2
    assert not st.eye_ready  # sin ninguno
    save_eye_setup(tmp_path, "regigigas", eye(), NormBox(0.4, 0.4, 0.1, 0.1), 1080)
    page.on_enter()
    assert page.eye_choice.currentData() == "regigigas_delante"  # se ofrece primero el que falta
    assert not st.eye_ready and "delante del legendario" in page.missing()
    save_eye_setup(tmp_path, "regigigas_delante", eye(), NormBox(0.45, 0.45, 0.1, 0.1), 1080)
    assert st.eye_ready and page.is_complete()
    assert load_eye_setup(tmp_path, "regigigas_delante") is not None


def test_normal_encounter_has_no_second_eye(app, tmp_path):
    st = AppState(tmp_path)
    st.auto_hardware = False
    st.selection.encounter = st.encounters["hansa_regice"]
    page = ScenePage(st)
    page.on_enter()
    assert page.eye_choice.count() == 0
