import random

import pytest

from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState, float_from_output


def reference_next(s):
    """Implementación literal de Project_Xs ``Xorshift.next`` (oráculo independiente)."""
    s0, s1, s2, s3 = s
    temp = s0 ^ s0 << 11 & 0xFFFFFFFF
    s0, s1, s2 = s1, s2, s3
    s3 = temp ^ temp >> 8 ^ s3 ^ s3 >> 19
    return (s0, s1, s2, s3), s3


def test_matches_project_xs_reference():
    rnd = random.Random(1234)
    for _ in range(50):
        words = tuple(rnd.getrandbits(32) for _ in range(4))
        rng = Xorshift128(*words)
        ref = words
        for _ in range(200):
            ref, out = reference_next(ref)
            assert rng.next() == out
        assert rng.state.words == ref


def test_prev_inverts_next():
    rnd = random.Random(99)
    for _ in range(50):
        rng = Xorshift128(*(rnd.getrandbits(32) for _ in range(4)))
        start = rng.state
        outs = rng.outputs(100)
        undone = [rng.prev() for _ in range(100)]
        assert rng.state == start
        assert undone == outs[::-1]


def test_negative_advance_goes_back():
    rng = Xorshift128(1, 2, 3, 4)
    start = rng.state
    rng.advance(500)
    rng.advance(-500)
    assert rng.state == start


def test_state_formats_roundtrip():
    state = XorshiftState(0x12345678, 0x9ABCDEF0, 0x0FEDCBA9, 0x87654321)
    hi, lo = state.to_hex64()
    assert hi == "123456789ABCDEF0" and lo == "0FEDCBA987654321"
    assert XorshiftState.from_hex64(hi, lo) == state
    assert XorshiftState.parse(state.to_hex32()) == state
    assert XorshiftState.parse(f"{hi}\n{lo}") == state
    with pytest.raises(ValueError):
        XorshiftState(1 << 32, 0, 0, 0)


def test_rand_float_matches_project_xs_formula():
    value = 0x00ABCDEF
    t = (value & 0x7FFFFF) / 8388607.0
    assert float_from_output(value, 3.0, 12.0) == pytest.approx(t * 3.0 + (1 - t) * 12.0)
    # extremos: t=0 → max, t=1 → min (interpolación invertida)
    assert float_from_output(0, 3.0, 12.0) == 12.0
    assert float_from_output(0x7FFFFF, 3.0, 12.0) == 3.0


def test_rand_range():
    rng = Xorshift128(5, 6, 7, 8)
    copy = rng.copy()
    assert rng.rand_range(10, 20) == copy.next() % 10 + 10
