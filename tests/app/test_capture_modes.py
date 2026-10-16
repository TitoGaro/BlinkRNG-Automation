"""Selección automática del modo de captura (sin abrir dispositivos reales)."""
from types import SimpleNamespace

import numpy as np
import pytest

from rngauto.app import session as session_mod
from rngauto.vision.frames import Frame, SourceInfo

# Capturadora USB 2.0 típica: 1080p solo a 30 FPS en MJPEG; sin comprimir, 5 FPS.
BEHAVIOUR = {
    (1920, 1080, 60, "MJPG"): (1920, 1080, 30.0, "MJPG"),
    (1920, 1080, 30, "MJPG"): (1920, 1080, 30.0, "MJPG"),
    (1280, 720, 60, "MJPG"): (1280, 720, 60.0, "MJPG"),
    (1920, 1080, 60, None): (1920, 1080, 5.0, "YUY2"),
    (1280, 720, 60, None): (1280, 720, 10.0, "YUY2"),
}
opened: list = []


class FakeDevice:
    def __init__(self, index, width, height, fps, fourcc=None, backend="msmf"):
        self.mode = BEHAVIOUR[(width, height, fps, fourcc)]
        self.closed = False
        opened.append(self)

    def open(self):
        w, h, fps, fcc = self.mode
        self.stats = SimpleNamespace(measured_fps=fps)
        return SourceInfo("fake", w, h, fps, fcc)

    def read(self, timeout_s=1.0):
        return Frame(np.zeros((self.mode[1], self.mode[0], 3), np.uint8), 0.0, 0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_capture(monkeypatch):
    import rngauto.vision.capture as cap

    opened.clear()
    monkeypatch.setattr(cap, "CaptureDevice", FakeDevice)
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    t = iter(range(10_000))
    monkeypatch.setattr(session_mod.time, "perf_counter", lambda: next(t) * 1.0)


def test_prefers_1080p30_mjpeg_over_720p60(fake_capture):
    s = session_mod.HardwareSession.open_capture(1)
    assert "1920x1080" in s.capture_label and "30 FPS" in s.capture_label
    # Solo queda abierto el dispositivo elegido
    assert sum(not d.closed for d in opened) == 1
    assert len(opened) == 2  # se detiene al tener 1080p fluido tras probar los dos modos MJPEG de 1080p


def test_rejects_too_slow_capture(fake_capture, monkeypatch):
    for k, v in list(BEHAVIOUR.items()):
        monkeypatch.setitem(BEHAVIOUR, k, (v[0], v[1], 8.0, v[3]))
    with pytest.raises(RuntimeError, match="FPS"):
        session_mod.HardwareSession.open_capture(1)
    assert all(d.closed for d in opened)
