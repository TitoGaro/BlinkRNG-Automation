"""Replay: grabar y re-procesar conserva los tiempos y los eventos detectados."""
from rngauto.games.bdsp.xorshift import XorshiftState
from rngauto.vision.blink_signal import EyeMatcher, EyeWatcher, auto_threshold, extract_blinks, group_blinks
from rngauto.vision.capture import CaptureDiagnostics
from rngauto.vision.replay import FrameRecorder, VideoFileSource, sidecar_path
from rngauto.vision.synthetic import SyntheticEyeScene, SyntheticRenderer, synthetic_frames

from .test_blink_signal import ground_truth


def detect(frames, matcher):
    watcher = EyeWatcher(matcher)
    for f in frames:
        watcher.process(f)
    th = auto_threshold(watcher.samples, min_duration_s=30, min_plateau_blinks=8)
    return group_blinks(extract_blinks(watcher.samples, th), th)


def test_record_and_replay_roundtrip(tmp_path):
    scene = SyntheticEyeScene(fps=30, noise_sigma=2)
    truth = ground_truth(XorshiftState(4, 3, 2, 1), 12)
    renderer = SyntheticRenderer(scene, 0)
    frames = list(synthetic_frames(scene, truth, truth[-1].t_s + 2, renderer=renderer))
    path = tmp_path / "sesion.avi"
    with FrameRecorder(path, scene.fps, (scene.width, scene.height), codec="MJPG") as rec:
        for f in frames:
            rec.write(f)
    assert sidecar_path(path).exists()
    with VideoFileSource(path) as src:
        replayed = list(src)
        assert src.info().width == scene.width
    assert len(replayed) == len(frames)
    assert [f.t_s for f in replayed] == [f.t_s for f in frames]
    matcher = EyeMatcher(renderer.template(), scene.eye_box, reference_height=scene.height)
    live = detect(frames, matcher)
    offline = detect(replayed, matcher)
    assert len(offline) == len(live) >= 11
    assert all(abs(a.t_s - b.t_s) < 0.02 and a.kind == b.kind for a, b in zip(live, offline))


def test_capture_diagnostics_messages():
    bad = CaptureDiagnostics(index=1, opened=True, frames=0)
    assert not bad.ok and "no se reciben frames" in bad.lines()[0]
    good = CaptureDiagnostics(1, True, 1920, 1080, "MJPG", 60, 59.9, 120, 1, 0, 2.0, 80.0)
    assert good.ok
    text = "\n".join(good.lines())
    assert "1920x1080" in text and "MJPEG" in text
