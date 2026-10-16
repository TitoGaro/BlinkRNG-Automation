"""Visión con datos sintéticos: detección de parpadeos y recuperación del estado de extremo a extremo."""
import random

import pytest

from rngauto.games.bdsp.blink_pipeline import recover_from_events
from rngauto.games.bdsp.scene import PLAYER, SceneModel, SceneSimulator
from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState
from rngauto.vision.blink_signal import (
    BlinkEventStream, EyeMatcher, EyeWatcher, auto_threshold, extract_blinks, group_blinks, signal_quality,
)
from rngauto.vision.frames import NormBox
from rngauto.vision.synthetic import GroundTruthBlink, SyntheticEyeScene, SyntheticRenderer, synthetic_frames


def ground_truth(state, count, period=1.0197, npc=0):
    sim = SceneSimulator.start(state, SceneModel(npc=npc, period_s=period), start_time_s=1.0)
    out = []
    while len(out) < count:
        for ev in sim.step():
            if ev.actor == PLAYER and ev.player_blink is not None:
                out.append(GroundTruthBlink(ev.time_s, ev.player_blink, ev.advance))
    return out


def run_pipeline(scene, truth, seed=0, upscale=1.0):
    renderer = SyntheticRenderer(scene, seed)
    matcher = EyeMatcher(renderer.template(), scene.eye_box, reference_height=scene.height, upscale=upscale)
    watcher = EyeWatcher(matcher)
    duration = truth[-1].t_s + 2.0
    for frame in synthetic_frames(scene, truth, duration, seed=seed, renderer=renderer):
        watcher.process(frame)
    return watcher.samples


def match_events(events, truth, tol=0.1):
    """(aciertos, falsos positivos, tipos correctos, error temporal máximo)."""
    hits, kinds, worst = 0, 0, 0.0
    used = set()
    for e in events:
        best = min(range(len(truth)), key=lambda i: abs(truth[i].t_s - e.t_s))
        err = abs(truth[best].t_s + 0.06 - e.t_s)  # el centroide cae en el centro del primer cierre
        if abs(truth[best].t_s - e.t_s) < tol + 0.1 and best not in used:
            used.add(best)
            hits += 1
            kinds += int(e.kind == truth[best].kind)
            worst = max(worst, err)
    return hits, len(events) - hits, kinds, worst


def test_matcher_separates_open_and_closed():
    scene = SyntheticEyeScene(noise_sigma=0)
    r = SyntheticRenderer(scene)
    m = EyeMatcher(r.template(), scene.eye_box, reference_height=scene.height)
    open_score, ok = m.score(r.render(1.0))
    closed_score, _ = m.score(r.render(0.0))
    assert ok and open_score > 0.95 and closed_score < open_score - 0.15


def test_normbox_roundtrip():
    box = NormBox.from_pixels(621, 343, 17, 19, 1280, 720)
    assert box.to_pixels(1280, 720) == (621, 343, 17, 19)
    assert box.expanded(2).w == pytest.approx(box.w * 2)


@pytest.mark.parametrize(
    "scene",
    [
        SyntheticEyeScene(fps=60, noise_sigma=2),
        SyntheticEyeScene(fps=30, noise_sigma=4, jpeg_quality=60),
        SyntheticEyeScene(fps=60, noise_sigma=3, jpeg_quality=50, eye_px=14, drop_prob=0.02, dup_prob=0.05),
    ],
    ids=["limpio60", "mjpeg30", "ojo-pequeño-mjpeg-drops"],
)
def test_detection_precision_recall(scene):
    truth = ground_truth(XorshiftState(1, 2, 3, 4), 30)
    samples = run_pipeline(scene, truth)
    th = auto_threshold(samples, min_duration_s=60)
    assert th is not None
    events = group_blinks(extract_blinks(samples, th), th)
    hits, false_pos, kinds, worst = match_events(events, truth)
    recall = hits / len(truth)
    assert recall >= 0.97, (recall, false_pos)
    assert false_pos == 0
    assert kinds >= hits - 1  # tipo simple/doble casi siempre correcto
    assert worst < 1.5 / scene.fps + 0.02  # error temporal < ~1,5 frames
    q = signal_quality(samples, th)
    assert q.snr > 5


@pytest.mark.slow
def test_end_to_end_video_to_state():
    """Estado conocido → vídeo sintético (MJPEG, 30 fps) → eventos → recuperación del estado."""
    rnd = random.Random(2026)
    state = XorshiftState(*(rnd.getrandbits(32) for _ in range(4)))
    truth = ground_truth(state, 42)
    scene = SyntheticEyeScene(fps=30, noise_sigma=3, jpeg_quality=70)
    samples = run_pipeline(scene, truth, seed=3)
    stream = BlinkEventStream(min_duration_s=120)
    for s in samples:
        stream.feed(s)
    events = stream.all_events()
    rec = recover_from_events(events, npc=0)
    expected = Xorshift128.from_state(state)
    expected.advance(truth[0].advance - 1)
    assert rec.result.state == expected.state
    assert rec.assignment.period_s == pytest.approx(1.0197, abs=0.002)
    # El reloj resultante predice el advance de un parpadeo posterior
    later = ground_truth(state, 50)[-1]
    rel = later.advance - (truth[0].advance - 1)
    assert rec.clock.advance_at(later.t_s + 0.5) == rel


def test_stream_poll_emits_each_event_once():
    truth = ground_truth(XorshiftState(9, 8, 7, 6), 25)
    samples = run_pipeline(SyntheticEyeScene(fps=30, noise_sigma=2), truth)
    stream = BlinkEventStream(min_duration_s=60)
    emitted = []
    for i, s in enumerate(samples):
        stream.feed(s)
        if i % 30 == 0:
            emitted.extend(stream.poll())
    emitted.extend(stream.poll())
    times = [e.t_s for e in emitted]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert len(emitted) >= 20


def test_second_close_in_the_ambiguous_zone_is_not_a_phantom_roll():
    """Doble con el segundo cierre a 0,58 s (zona dudosa): un solo evento de tipo desconocido, no dos."""
    from rngauto.vision.blink_signal import Blink

    def b(t):
        return Blink(t_s=t, depth=0.5, frames=3, duration_s=0.05, valid_ratio=1.0)

    events = group_blinks([b(10.00), b(10.58), b(12.04)], threshold=0.1)
    assert [round(e.t_s, 2) for e in events] == [10.00, 12.04]
    assert events[0].kind is None


def test_final_events_leave_out_a_double_blink_still_in_progress():
    """Un doble del que solo ha llegado el primer cierre parecería simple: hasta que es definitivo no se usa."""
    from rngauto.vision.blink_signal import BlinkEventStream, MatchSample

    def score(t):  # simple a 5 s y doble a 10,00 + 10,35 s
        return 0.5 if any(c <= t < c + 0.1 for c in (5.0, 10.0, 10.35)) else 0.9

    def stream_until(t_end):
        s = BlinkEventStream(fixed_threshold=0.2)
        i = 0
        while i / 60 <= t_end:
            s.feed(MatchSample(i / 60, score(i / 60), True, i))
            i += 1
        return s

    partial = stream_until(10.2)  # aún no ha llegado el segundo cierre
    assert [round(e.t_s) for e in partial.final_events()] == [5]
    done = stream_until(11.5)
    assert [e.kind.name for e in done.final_events()] == ["SINGLE", "DOUBLE"]
