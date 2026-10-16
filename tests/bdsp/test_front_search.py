"""Reidentificación delante del legendario (``_front_search``, la parte que va en un hilo aparte)."""
from types import SimpleNamespace

from rngauto.games.bdsp.blink import player_blink_kind
from rngauto.games.bdsp.scene import PLAYER
from rngauto.games.bdsp.static_run.front import _front_search

from .test_reidentify_pokemon import PERIOD, scene


def _blinks(events, start: int, count: int):
    """Parpadeos del jugador (como los da el detector: instante y tipo) a partir del ``start``-ésimo."""
    player = [e for e in events if e.actor == PLAYER and player_blink_kind(e.output) is not None]
    chosen = player[start:start + count]
    return chosen[0], [SimpleNamespace(t_s=e.time_s, kind=player_blink_kind(e.output)) for e in chosen]


def _rec(base, expected_advance: int):
    clock = SimpleNamespace(period_s=PERIOD, advance_at=lambda t: expected_advance)
    return SimpleNamespace(clock=clock, result=SimpleNamespace(state=base))


def test_finds_the_state_near_the_expected_advance():
    base, _sim, events = scene(2, 900)
    first, blinks = _blinks(events, 40, 20)
    found, window, period, t0, errors = _front_search(
        _rec(base, first.advance - 120), blinks, blinks=12, until_t=blinks[-1].t_s, abs_window=None,
        enc_window=(-300, 300), started=blinks[0].t_s - 1.0, scene_rate=1 / PERIOD + 1 / 7.785)
    assert found is not None and found.first_advance == first.advance
    assert len(window) == 20 and abs(period - PERIOD) < 0.01 and abs(t0 - first.time_s) < 0.2
    assert errors == []


def test_outside_the_window_reports_every_try_instead_of_raising():
    base, _sim, events = scene(3, 900)
    first, blinks = _blinks(events, 40, 16)
    far = first.advance + 50_000
    found, _window, _period, _t0, errors = _front_search(
        _rec(base, far), blinks, blinks=12, until_t=blinks[-1].t_s, abs_window=(far, far + 200, blinks[0].t_s),
        enc_window=(-300, 300), started=blinks[0].t_s - 1.0, scene_rate=1 / PERIOD + 1 / 7.785)
    assert found is None
    assert [n for n, _exc in errors] == [16, 12]  # todos los parpadeos y los 12 últimos (sin repetir)


def test_a_failed_pokedex_action_is_retried_after_a_restart():
    """Un fallo puntual de la Pokédex no la descarta para siempre (cada intento posterior se perdería), pero
    tras ``FAST_FAIL_MAX`` fallos ya no se vuelve a usar; y si el usuario la rechazó, no se vuelve a ofrecer."""
    from rngauto.games.bdsp.static_run.config import FAST_FAIL_MAX
    from rngauto.games.bdsp.static_strategy import Mode, StaticEncounterRun

    run = object.__new__(StaticEncounterRun)
    run.cfg = SimpleNamespace(max_attempts=100, reset_from_home=False, mode=Mode.AUTOMATIC)
    run.d = SimpleNamespace(reset_game=lambda: None)
    run._emit = lambda **_k: None
    run.attempt_no, run._battle_possible = 0, False
    run._fast_disabled, run._fast_failures, run._fast_declined = set(), {}, False
    ctx = SimpleNamespace(log=lambda *_a, **_k: None, check=lambda: None)

    for n in range(1, FAST_FAIL_MAX + 1):
        run._fast_failed("pokedex")
        assert "pokedex" in run._fast_disabled  # en el mismo intento, no se repite
        assert run.reset(ctx) == "COLLECT"
        assert ("pokedex" in run._fast_disabled) is (n >= FAST_FAIL_MAX)

    run._fast_disabled, run._fast_failures = {"pokedex"}, {"pokedex": 1}
    run._fast_declined = True
    run.reset(ctx)
    assert "pokedex" in run._fast_disabled
