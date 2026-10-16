"""Stats ↔ IVs (fórmulas de Gen 3+; sin EVs para un Pokémon recién capturado).

Sirve para verificar el resultado de un intento leyendo los stats de la pantalla de datos: de los
stats observados se deducen rangos de IV, que identifican el advance obtenido.
"""
from __future__ import annotations

from rngauto.core.pokemon import IVRange
from rngauto.i18n import t

# Orden de la tabla de naturalezas: +stat = n // 5, −stat = n % 5 sobre (Atk, Def, Spe, SpA, SpD).
# Se traduce a nuestro orden de IVs (HP, Atk, Def, SpA, SpD, Spe).
_NATURE_ORDER_TO_IV = (1, 2, 5, 3, 4)


def nature_modifiers(nature: int) -> tuple[int, ...]:
    """Modificador por stat en décimas: 10 neutro, 11 aumenta, 9 disminuye (HP siempre 10)."""
    mods = [10] * 6
    up, down = _NATURE_ORDER_TO_IV[nature // 5], _NATURE_ORDER_TO_IV[nature % 5]
    if up != down:
        mods[up] = 11
        mods[down] = 9
    return tuple(mods)


def compute_stat(index: int, base: int, iv: int, level: int, mod: int = 10, ev: int = 0) -> int:
    """Stat ``index`` (0 = PS) con la fórmula del juego; ``mod`` en décimas, como ``nature_modifiers``.

    Las divisiones enteras reproducen los truncamientos del juego, que importan para cuadrar el valor exacto."""
    core = (2 * base + iv + ev // 4) * level // 100
    if index == 0:
        return core + level + 10
    return (core + 5) * mod // 10


def compute_stats(base: tuple[int, ...], ivs: tuple[int, ...], level: int, nature: int) -> tuple[int, ...]:
    """Las seis estadísticas sin EVs."""
    mods = nature_modifiers(nature)
    return tuple(compute_stat(i, base[i], ivs[i], level, mods[i]) for i in range(6))


class StatsInconsistent(ValueError):
    """Ningún IV produce el stat leído (lectura errónea o naturaleza/nivel incorrectos)."""


def iv_ranges_from_stats(base: tuple[int, ...], level: int, nature: int, stats: tuple[int, ...]) -> tuple[IVRange, ...]:
    """Rango de IV compatible con cada stat leído (por fuerza bruta: solo hay 32 valores).

    A nivel bajo varios IV dan el mismo stat, por eso se devuelve un rango y no un valor."""
    mods = nature_modifiers(nature)
    ranges = []
    for i in range(6):
        ok = [iv for iv in range(32) if compute_stat(i, base[i], iv, level, mods[i]) == stats[i]]
        if not ok:
            raise StatsInconsistent(t("El stat {i} = {value} es imposible con nivel {level} y esa naturaleza",
                                      "Stat {i} = {value} is impossible at level {level} with that nature",
                                      i=i, value=stats[i], level=level))
        ranges.append(IVRange(min(ok), max(ok)))
    return tuple(ranges)
