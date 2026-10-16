"""Reidentificación con un Pokémon parpadeando (Regigigas): contra escenas simuladas exactas."""
import random

import pytest

from rngauto.games.bdsp.blink import player_blink_kind
from rngauto.games.bdsp.recovery import RecoveryError
from rngauto.games.bdsp.reidentify_pokemon import TickObservation, certain_until, reidentify_with_pokemon
from rngauto.games.bdsp.scene import PLAYER, SceneModel, SceneSimulator
from rngauto.games.bdsp.xorshift import XorshiftState

PERIOD = 1.0195


def scene(seed: int, seconds: float):
    rnd = random.Random(seed)
    base = XorshiftState(*(rnd.getrandbits(32) | 1 for _ in range(4)))
    sim = SceneSimulator.start(base, SceneModel(pokemon_npc=1, period_s=PERIOD), first_roll_s=PERIOD)
    events = sim.run_until(seconds)
    return base, sim, events


def observe(events, start_roll: int, blinks: int):
    """Tiradas del jugador desde la ``start_roll``-ésima que parpadea, hasta reunir ``blinks`` parpadeos."""
    player = [e for e in events if e.actor == PLAYER]
    blink_rolls = [i for i, e in enumerate(player) if player_blink_kind(e.output) is not None]
    first = blink_rolls[start_roll]
    last = blink_rolls[start_roll + blinks - 1]
    obs = [TickObservation(i - first, (k := player_blink_kind(player[i].output)) is not None, k)
           for i in range(first, last + 1)]
    return player[first], obs


@pytest.mark.parametrize("seed", range(6))
def test_finds_advance_and_pokemon_timing(seed):
    base, sim, events = scene(seed, 900)
    tick0, obs = observe(events, start_roll=40, blinks=20)
    res = reidentify_with_pokemon(base, obs, PERIOD, tick0.advance - 300, tick0.advance + 300)
    assert res.first_advance == tick0.advance
    # Siguiente parpadeo del Pokémon dentro del margen calculado.
    last_t = tick0.time_s + (len(obs) - 1) * PERIOD
    tail = [e for e in events if e.time_s > last_t + 1e-6]
    real_pk = next(e.time_s for e in tail if e.actor != PLAYER) - tick0.time_s
    lo, hi = res.pokemon_window_s
    assert lo - 0.06 <= real_pk <= hi + 0.06
    # Y la escena predicha coincide con la real hasta el primer «empate» (Pokémon pegado a una tirada), que es
    # hasta donde el plan puede fiarse de ella (``certain_until``).
    pred = res.simulator(tick0.time_s, PERIOD)
    horizon = certain_until(pred.fork(), tick0.time_s, res, until_s=events[-1].time_s)
    predicted = [e for e in pred.run_until(horizon)]
    real = [e for e in tail if e.time_s <= horizon]
    assert [(e.advance, e.actor) for e in predicted] == [(e.advance, e.actor) for e in real]
    assert horizon > last_t


def test_too_few_blinks_is_ambiguous_or_refused():
    base, sim, events = scene(1, 600)
    tick0, obs = observe(events, start_roll=10, blinks=4)
    with pytest.raises(RecoveryError):
        reidentify_with_pokemon(base, obs, PERIOD, 0, 20_000)


def test_plan_in_scene_hits_the_target():
    """El plan sobre la escena predicha lleva al objetivo en una simulación completa (espera + timeline)."""
    from rngauto.core.encounter import FinalAction, SceneSpec, TimelinePhase
    from rngauto.games.bdsp.planner import plan_static_in_scene
    from rngauto.games.bdsp.xorshift import Xorshift128

    base, sim, events = scene(2, 900)
    tick0, obs = observe(events, start_roll=40, blinks=20)
    res = reidentify_with_pokemon(base, obs, PERIOD, tick0.advance - 300, tick0.advance + 300)
    idle = res.simulator(tick0.time_s, PERIOD)
    now = idle.time_s
    horizon = certain_until(idle.fork(), tick0.time_s, res, until_s=now + 300)
    phase = TimelinePhase("t", "A", blank_reads=2, pause_s=2.0, fixed_delay=0, scene=SceneSpec(pokemon_npc=1))
    final = FinalAction("A", final_delay=50, min_timeline_s=15.0, min_gap_margin_s=0.12)
    # Un objetivo alcanzable: lo que habría ~25 s después + el delay.
    target = idle.fork()
    target.run_until(min(horizon, now + 20))
    plan = plan_static_in_scene(idle, horizon, (phase,), final, target.advance + 30 + 50, now_s=now, prep_s=5)
    t1, c1 = plan.presses[0].video_t, plan.presses[0].expected_count
    # Comprobación independiente: escena real hasta t1 → cuenta c1; timeline real hasta t2 → objetivo − delay.
    real_c1 = max(e.advance for e in events if e.time_s < t1)
    assert real_c1 == c1
    rng = Xorshift128.from_state(base)
    rng.advance(c1 + 2)
    tl = SceneSimulator.start(rng.state, SceneModel(pokemon_npc=1, period_s=PERIOD), start_time_s=t1 + 2.0,
                              start_advance=c1 + 2)
    tl.run_until(plan.final.video_t)
    assert tl.advance == plan.target_advance - 50


def test_single_press_plan_in_scene():
    """Sin timeline (Cresselia): la única pulsación cae cuando la escena real vale objetivo − delay."""
    from rngauto.core.encounter import FinalAction
    from rngauto.games.bdsp.planner import plan_static_in_scene

    base, sim, events = scene(3, 900)
    tick0, obs = observe(events, start_roll=40, blinks=20)
    res = reidentify_with_pokemon(base, obs, PERIOD, tick0.advance - 300, tick0.advance + 300)
    idle = res.simulator(tick0.time_s, PERIOD)
    now = idle.time_s
    horizon = certain_until(idle.fork(), tick0.time_s, res, until_s=now + 300)
    final = FinalAction("A", final_delay=103, min_timeline_s=0.0, min_gap_margin_s=0.12)
    probe = idle.fork()
    probe.run_until(min(horizon, now + 15))
    plan = plan_static_in_scene(idle, horizon, (), final, probe.advance + 103, now_s=now, prep_s=5)
    t = plan.final.video_t
    assert max(e.advance for e in events if e.time_s < t) == plan.final.expected_count
