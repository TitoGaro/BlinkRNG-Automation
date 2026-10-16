"""Zonas que la auditoría de 2026-10 encontró sin tests: shiny-lock, ajuste de tiradas y ayudas de la
reidentificación delante del Pokémon."""
import pytest

from rngauto.core.observation import BlinkKind
from rngauto.core.pokemon import Shiny
from rngauto.games.bdsp.blink import assign_rolls
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.recovery import RecoveryError
from rngauto.games.bdsp.reidentify_pokemon import fit_period, ticks_from_blinks
from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState


@pytest.mark.parametrize("key", ["jirachi", "mew"])
def test_shiny_locked_templates_are_never_shiny(key):
    """Con cualquier TSV (incluido el que haría shiny el primer PID), Mew y Jirachi nunca salen shiny."""
    finder = StaticTargetFinder(static_template(key))
    state = XorshiftState.from_hex64(0x1234567887654321, 0x8765432112345678)
    for tsv in (0, 1234, 4095):
        finder.tsv = tsv
        assert all(finder.generate(state, adv).shiny is Shiny.NONE for adv in range(0, 4000, 7))


def test_assign_rolls_phase_matches_rolls_with_an_outlier_first_blink():
    """El primer parpadeo desviado medio periodo no debe dejar la fase desfasada tiradas enteras."""
    period = 1.0197
    times = [0.0 + 0.45] + [period * k for k in range(1, 30)]
    a = assign_rolls(times, period_prior_s=period)
    assert a.rolls == tuple(sorted(set(a.rolls)))  # estrictamente creciente
    # Fase y periodo coherentes con las tiradas devueltas: residuos acotados (en fracciones de periodo).
    assert max(abs(r) for r in a.residuals[1:]) < 0.1
    assert abs(a.phase_s + a.period_s * a.rolls[5] - times[5]) < 0.1 * period


def test_ticks_from_blinks_marks_every_roll():
    period = 1.0
    ticks = ticks_from_blinks([10.0, 12.02, 15.97], [BlinkKind.SINGLE, BlinkKind.DOUBLE, None], 10.0, period, 17.2)
    assert [t.roll for t in ticks] == list(range(8))
    assert [t.roll for t in ticks if t.blink] == [0, 2, 6]
    assert ticks[2].kind is BlinkKind.DOUBLE and ticks[6].kind is None


def test_tick_helpers_reject_empty_input():
    with pytest.raises(RecoveryError):
        ticks_from_blinks([], [], 0.0, 1.0, 10.0)
    with pytest.raises(RecoveryError):
        fit_period([], 1.0197)


def test_fit_period_recovers_a_slightly_different_period():
    true = 1.0213
    times = [5.0 + true * k for k in (0, 3, 4, 9, 15, 22, 40, 41, 77, 120)]
    period, _phase = fit_period(times, 1.0197)
    assert period == pytest.approx(true, abs=1e-4)


def test_skip_matches_advance_both_ways():
    base = Xorshift128.from_state(XorshiftState.from_hex64(0x0123456789ABCDEF, 0xFEDCBA9876543210))
    for n in (0, 1, 999, 54321, -7):
        a, b = Xorshift128.from_state(base.state), Xorshift128.from_state(base.state)
        a.advance(n)
        b.skip(n)
        assert a.state == b.state


def test_red_pokemon_is_not_mistaken_for_a_bag_pocket():
    """Ho-Oh (rojo) en pantalla antes de abrirse la Bolsa: no es la pestaña de las Poké Balls (2026-10-09)."""
    import numpy as np

    from rngauto.games.bdsp.screens import BAG_TAB_X, BAG_TAB_Y, bag_pocket

    img = np.full((1080, 1920, 3), (40, 120, 200), np.uint8)  # fondo anaranjado, como el combate en Hansa
    cx, cy = int(BAG_TAB_X[1] * 1920), int(BAG_TAB_Y * 1080)
    img[cy - 40:cy + 40, cx - 40:cx + 40] = (30, 30, 230)  # rojo en la pestaña de las Poké Balls
    assert bag_pocket(img) is None
