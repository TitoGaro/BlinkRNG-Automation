"""Modelo de parpadeo de BDSP y conversión de tiempos observados a índices de tirada.

Modelo:
  * El jugador (y cada NPC humano) hace una tirada ``r = next()`` cada ~1.02 s.
    Parpadea si ``(r & 0b1110) == 0`` (1/8) y el tipo es ``r & 1`` (0 simple, 1 doble).
  * Cada "Pokémon NPC" (modelo de legendario, Munchlax, Starly...) consume una llamada al
    parpadear y programa el siguiente parpadeo a ``rand_float(3, 12) + 0.285`` s.

Periodo: el valor nominal es 1.017 s, pero en consola real mide unos 1.0197 s. Con 1.017 fijo el contador
deriva ~1 tirada cada ~6 min. Aquí 1.0197 es solo el *prior*: :func:`assign_rolls` lo estima a partir de las
observaciones.
"""
from __future__ import annotations

from dataclasses import dataclass

from rngauto.core.gf2 import SymbolicWord
from rngauto.core.observation import BlinkKind
from rngauto.i18n import t

from .xorshift import float_from_output

PLAYER_BLINK_MASK = 0b1110
DEFAULT_ROLL_PERIOD_S = 1.0197  # medido en consola real; se re-estima en cada sesión
POKEMON_BLINK_MIN_S = 3.0
POKEMON_BLINK_MAX_S = 12.0
POKEMON_BLINK_EXTRA_S = 0.285


def is_player_blink(value: int) -> bool:
    """¿La salida ``value`` hace parpadear al jugador? Pasa 1 de cada 8 tiradas."""
    return (value & PLAYER_BLINK_MASK) == 0


def player_blink_kind(value: int) -> BlinkKind | None:
    """Tipo de parpadeo que produce una tirada, o ``None`` si no parpadea."""
    if not is_player_blink(value):
        return None
    return BlinkKind(value & 1)


def pokemon_blink_interval(value: int) -> float:
    """Segundos hasta el siguiente parpadeo de un Pokémon NPC a partir de la salida consumida."""
    return float_from_output(value, POKEMON_BLINK_MIN_S, POKEMON_BLINK_MAX_S) + POKEMON_BLINK_EXTRA_S


class SymbolicXorshift:
    """Xorshift128 simbólico: cada bit de salida es una máscara lineal del estado inicial."""

    def __init__(self) -> None:
        self.words = [SymbolicWord.variable(i) for i in range(4)]

    def next(self) -> SymbolicWord:
        """Mismo paso que ``Xorshift128.next`` pero sobre máscaras: devuelve la palabra de salida simbólica."""
        s0, s1, s2, s3 = self.words
        t = s0 ^ s0.shl(11)
        new = t ^ t.shr(8) ^ s3 ^ s3.shr(19)
        self.words = [s1, s2, s3, new]
        return new


class LowNibbleStream:
    """Máscaras de los 4 bits bajos de cada salida (posición 0 = primera llamada a ``next()``).

    Se calcula de forma perezosa y se cachea, de modo que probar exclusiones o desplazamientos de
    observaciones no requiere regenerar nada.
    """

    def __init__(self) -> None:
        self._sym = SymbolicXorshift()
        self._nibbles: list[tuple[int, int, int, int]] = []

    def at(self, position: int) -> tuple[int, int, int, int]:
        """Máscaras de los bits 0-3 de la salida ``position``. El bit 0 da el tipo de parpadeo y los bits 1-3
        deciden si parpadea: son los únicos que se observan."""
        while len(self._nibbles) <= position:
            word = self._sym.next()
            self._nibbles.append((word.bit(0), word.bit(1), word.bit(2), word.bit(3)))
        return self._nibbles[position]


# --- Tiempos observados → índices de tirada ----------------------------------------------------

@dataclass(frozen=True, slots=True)
class RollAssignment:
    """Resultado de :func:`assign_rolls`: índice de tirada de cada parpadeo, periodo y fase ajustados (la fase es
    el instante absoluto de la tirada 0) y el residuo de cada observación respecto al ajuste."""

    rolls: tuple[int, ...]
    period_s: float
    phase_s: float
    residuals: tuple[float, ...]  # en fracciones de periodo

    @property
    def max_abs_residual(self) -> float:
        """Mayor desviación respecto al ajuste, en fracciones de periodo (0,5 = a mitad de camino entre dos)."""
        return max((abs(r) for r in self.residuals), default=0.0)

    def ambiguous(self, threshold: float = 0.3) -> tuple[int, ...]:
        """Índices de observaciones cuya asignación de tirada es dudosa."""
        return tuple(i for i, r in enumerate(self.residuals) if abs(r) > threshold)


def assign_rolls(
    times_s: list[float],
    period_prior_s: float = DEFAULT_ROLL_PERIOD_S,
    period_tolerance: float = 0.02,
    iterations: int = 4,
) -> RollAssignment:
    """Asigna a cada parpadeo observado su índice de tirada del jugador (el primero es 0).

    1. Redondeo por intervalos con el periodo *prior* (robusto ante error de periodo).
    2. Ajuste por mínimos cuadrados ``t = fase + periodo·k`` (periodo acotado a ±tolerancia).
    3. Re-asignación absoluta con el ajuste (corrige errores aislados) e iteración.

    Para un doble parpadeo, ``times_s`` debe ser el instante del primer cierre.
    """
    if not times_s:
        return RollAssignment((), period_prior_s, 0.0, ())
    if any(b < a for a, b in zip(times_s, times_s[1:])):
        raise ValueError(t("Los tiempos deben estar ordenados", "Times must be sorted"))
    t0 = times_s[0]
    rel = [t - t0 for t in times_s]
    rolls = [0]
    for a, b in zip(rel, rel[1:]):
        rolls.append(rolls[-1] + max(1, round((b - a) / period_prior_s)))
    period, phase = period_prior_s, 0.0
    lo, hi = period_prior_s * (1 - period_tolerance), period_prior_s * (1 + period_tolerance)
    def fit(ks: list[int], fallback: float) -> tuple[float, float]:
        """Periodo acotado a ±tolerancia y la fase que mejor encaja CON ese periodo (no la del ajuste libre)."""
        p, _ = _fit_line(ks, rel, fallback)
        p = min(max(p, lo), hi)
        return p, sum(rel) / len(rel) - p * sum(ks) / len(ks)

    for _ in range(iterations):
        period, phase = fit(rolls, period)
        new_rolls = [round((t - phase) / period) for t in rel]
        offset = new_rolls[0]
        new_rolls = [k - offset for k in new_rolls]
        if not _strictly_increasing(new_rolls) or new_rolls == rolls:
            break
        rolls = new_rolls
    period, phase = fit(rolls, period)  # periodo y fase siempre coherentes con las tiradas devueltas
    residuals = tuple((t - (phase + period * k)) / period for t, k in zip(rel, rolls))
    return RollAssignment(tuple(rolls), period, phase + t0, residuals)


def _fit_line(ks: list[int], ts: list[float], fallback_period: float) -> tuple[float, float]:
    """Recta ``t = fase + periodo·k`` por mínimos cuadrados; devuelve ``(periodo, fase)``. Con menos de dos
    tiradas distintas no hay pendiente que ajustar y se usa ``fallback_period``."""
    n = len(ks)
    if n < 2 or ks[-1] == ks[0]:
        return fallback_period, ts[0] - fallback_period * ks[0]
    mk = sum(ks) / n
    mt = sum(ts) / n
    sxx = sum((k - mk) ** 2 for k in ks)
    sxy = sum((k - mk) * (t - mt) for k, t in zip(ks, ts))
    period = sxy / sxx
    return period, mt - period * mk


def _strictly_increasing(values: list[int]) -> bool:
    return all(b > a for a, b in zip(values, values[1:]))
