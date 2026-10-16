"""Recuperación del estado a partir de parpadeos simulados (sin Switch)."""
import random

import pytest

from rngauto.games.bdsp.blink import BlinkKind, assign_rolls
from rngauto.games.bdsp.recovery import (
    BlinkObservation,
    RecoveryError,
    recover_state,
    reidentify,
    validate,
)
from rngauto.games.bdsp.scene import PLAYER, SceneModel, SceneSimulator
from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState


def random_state(seed: int) -> XorshiftState:
    rnd = random.Random(seed)
    return XorshiftState(*(rnd.getrandbits(32) for _ in range(4)))


def simulate_player_blinks(state, model, count, start_time=0.0):
    """Devuelve [(tiempo, tipo, advance)] de los ``count`` primeros parpadeos del jugador."""
    sim = SceneSimulator.start(state, model, start_time_s=start_time, schedule_pokemon_now=True)
    blinks = []
    while len(blinks) < count:
        for ev in sim.step():
            if ev.actor == PLAYER and ev.player_blink is not None:
                blinks.append((ev.time_s, ev.player_blink, ev.advance))
    return blinks


@pytest.mark.parametrize("npc", [0, 1, 3])
def test_recover_exact_rolls(npc):
    state = random_state(10 + npc)
    model = SceneModel(npc=npc, player_phase=npc // 2)
    blinks = simulate_player_blinks(state, model, 40)
    times = [t for t, _, _ in blinks]
    rolls = assign_rolls(times, period_prior_s=model.period_s).rolls
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(rolls, blinks)]
    result = recover_state(obs, npc=npc)
    # El estado recuperado es el previo a la tirada del primer parpadeo
    first_advance = blinks[0][2]
    rng = Xorshift128.from_state(state)
    rng.advance(first_advance - 1)
    assert result.state == rng.state
    assert not result.mismatches
    assert result.excluded == ()
    assert result.redundant_equations == 4 * 40 - 128
    # Estado tras el último parpadeo == estado del simulador en ese advance
    rng2 = Xorshift128.from_state(state)
    rng2.advance(blinks[-1][2])
    assert result.state_after_roll(result.last_roll) == rng2.state


def test_recover_with_timing_noise_and_period_error():
    state = random_state(42)
    true_period = 1.0172
    model = SceneModel(period_s=true_period)
    blinks = simulate_player_blinks(state, model, 40)
    rnd = random.Random(5)
    # Jitter de detección ±60 ms (≈ 4 frames a 60 fps) y prior de periodo erróneo en 0,3 %
    times = [t + rnd.uniform(-0.06, 0.06) for t, _, _ in blinks]
    assignment = assign_rolls(times, period_prior_s=1.014)
    assert assignment.period_s == pytest.approx(true_period, abs=0.002)
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(assignment.rolls, blinks)]
    result = recover_state(obs, ambiguous=assignment.ambiguous())
    rng = Xorshift128.from_state(state)
    rng.advance(blinks[0][2] - 1)
    assert result.state == rng.state


def test_recover_excludes_false_blink():
    state = random_state(77)
    model = SceneModel()
    blinks = simulate_player_blinks(state, model, 42)
    rolls = assign_rolls([t for t, _, _ in blinks]).rolls
    obs = [BlinkObservation(r, k, 0.99) for r, (_, k, _) in zip(rolls, blinks)]
    # Falso positivo entre dos parpadeos reales, con baja confianza
    gap = next(i for i in range(1, len(obs)) if obs[i].roll - obs[i - 1].roll >= 3)
    fake = BlinkObservation(obs[gap - 1].roll + 1, BlinkKind.SINGLE, 0.4)
    corrupted = obs[:gap] + [fake] + obs[gap:]
    result = recover_state(corrupted)
    rng = Xorshift128.from_state(state)
    rng.advance(blinks[0][2] - 1)
    assert result.state == rng.state
    assert len(result.excluded) == 1
    assert result.confidence < 1.0


def test_recover_tolerates_missed_detection():
    state = random_state(8)
    blinks = simulate_player_blinks(state, SceneModel(), 42)
    rolls = assign_rolls([t for t, _, _ in blinks]).rolls
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(rolls, blinks)]
    del obs[10]  # el detector no vio un parpadeo
    result = recover_state(obs)
    assert len(result.mismatches) == 1 and result.mismatches[0].is_missed_detection
    assert not result.hard_mismatches


def test_wrong_blink_kind_is_detected_or_fixed():
    state = random_state(9)
    blinks = simulate_player_blinks(state, SceneModel(), 40)
    rolls = assign_rolls([t for t, _, _ in blinks]).rolls
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(rolls, blinks)]
    bad = obs[20]
    obs[20] = BlinkObservation(bad.roll, BlinkKind(1 - bad.kind), 0.3)
    result = recover_state(obs)
    assert 20 in result.excluded


def test_too_few_blinks():
    with pytest.raises(RecoveryError):
        recover_state([BlinkObservation(i * 8, BlinkKind.SINGLE) for i in range(10)])


def test_validate_reports_mismatch():
    state = random_state(1)
    rng = Xorshift128.from_state(state)
    values = rng.outputs(200)
    from rngauto.games.bdsp.blink import player_blink_kind

    obs = [BlinkObservation(i, player_blink_kind(v)) for i, v in enumerate(values) if player_blink_kind(v) is not None]
    assert validate(state, obs) == []
    obs.pop(3)
    assert len(validate(state, obs)) == 1


def test_reidentify_finds_advance():
    base = random_state(123)
    rng = Xorshift128.from_state(base)
    rng.advance(54_321)
    sim = SceneSimulator.start(rng.state, SceneModel(), schedule_pokemon_now=False)
    blinks = []
    while len(blinks) < 10:
        for ev in sim.step():
            if ev.player_blink is not None:
                blinks.append((ev.time_s, ev.player_blink, ev.advance))
    rolls = assign_rolls([t for t, _, _ in blinks]).rolls
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(rolls, blinks)]
    res = reidentify(base, obs, search_max=100_000)
    assert res.advance == 54_321 + blinks[-1][2]
    assert res.state == sim.state


def test_reidentify_with_npc():
    base = random_state(321)
    rng = Xorshift128.from_state(base)
    rng.advance(10_001)
    sim = SceneSimulator.start(rng.state, SceneModel(npc=1, player_phase=1), schedule_pokemon_now=False)
    blinks = []
    while len(blinks) < 12:
        for ev in sim.step():
            if ev.player_blink is not None:
                blinks.append((ev.time_s, ev.player_blink, ev.advance))
    rolls = assign_rolls([t for t, _, _ in blinks]).rolls
    obs = [BlinkObservation(r, k) for r, (_, k, _) in zip(rolls, blinks)]
    res = reidentify(base, obs, npc=1, search_max=50_000)
    assert res.state == Xorshift128.from_state(rng.state).state or res.advance == 10_001 + blinks[-1][2]
    check = Xorshift128.from_state(base)
    check.advance(res.advance)
    assert check.state == res.state
    assert res.advance == 10_001 + blinks[-1][2]
