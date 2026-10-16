import random

from rngauto.core.gf2 import LinearSystem, SymbolicWord, parity
from rngauto.games.bdsp.blink import SymbolicXorshift
from rngauto.games.bdsp.xorshift import Xorshift128


def test_symbolic_xorshift_evaluates_like_concrete():
    rnd = random.Random(7)
    words = [rnd.getrandbits(32) for _ in range(4)]
    assignment = (words[0] << 96) | (words[1] << 64) | (words[2] << 32) | words[3]
    sym = SymbolicXorshift()
    rng = Xorshift128(*words)
    for _ in range(300):
        assert sym.next().evaluate(assignment) == rng.next()


def test_linear_system_solves_random_full_rank():
    rnd = random.Random(3)
    secret = rnd.getrandbits(64)
    system = LinearSystem(64)
    while not system.is_determined:
        mask = rnd.getrandbits(64)
        system.add(mask, parity(mask & secret))
    assert system.solve() == secret
    # Ecuaciones extra coherentes → redundantes; una incoherente → conflicto
    mask = rnd.getrandbits(64) | 1
    assert system.add(mask, parity(mask & secret)) == "redundant"
    assert system.add(mask, parity(mask & secret) ^ 1) == "conflict"
    assert system.conflicts == 1


def test_underdetermined_returns_none():
    system = LinearSystem(8)
    system.add(0b1, 1)
    assert system.solve() is None


def test_symbolic_word_shifts():
    w = SymbolicWord.variable(3)  # s3: variables 0..31
    assert w.shl(1).bits[0] == 0 and w.shl(1).bits[1] == 1
    assert w.shr(1).bits[31] == 0 and w.shr(1).bits[0] == 1 << 1
