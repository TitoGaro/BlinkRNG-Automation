"""Xorshift128 tal y como lo usa Pokémon BDSP.

Además de avanzar (``next``, y ``jump`` en O(log n)) permite retroceder: ``prev()`` invierte cada paso del
xorshift y recupera las cuatro palabras anteriores.

Convenciones de estado (las dos formas habituales de escribir una semilla):
  * ``S[0..3]``: cuatro palabras u32 ``s0, s1, s2, s3``.
  * ``S[0-1]``: dos u64 ``(s0 << 32) | s1`` y ``(s2 << 32) | s3``.
"""
from __future__ import annotations

from dataclasses import dataclass

from rngauto.i18n import t

MASK32 = 0xFFFFFFFF
# ``rand_float`` del juego usa los 23 bits bajos de la salida como fracción en [0, 1].
FLOAT_MANTISSA_MASK = 0x7FFFFF
FLOAT_DIVISOR = 8388607.0


@dataclass(frozen=True, slots=True)
class XorshiftState:
    """Estado inmutable de 128 bits (valor serializable / comparable)."""

    s0: int
    s1: int
    s2: int
    s3: int

    def __post_init__(self) -> None:
        for name in ("s0", "s1", "s2", "s3"):
            value = getattr(self, name)
            if not 0 <= value <= MASK32:
                raise ValueError(t("{name} fuera de rango u32: {value!r}", "{name} out of u32 range: {value!r}",
                                   name=name, value=value))

    @classmethod
    def from_words(cls, words) -> "XorshiftState":
        """Construye desde el formato ``S[0..3]`` (cuatro u32 en cualquier iterable)."""
        s0, s1, s2, s3 = (int(w) for w in words)
        return cls(s0, s1, s2, s3)

    @classmethod
    def from_hex64(cls, high: str | int, low: str | int) -> "XorshiftState":
        """Construye desde el formato ``S[0-1]`` (dos u64)."""
        hi = int(high, 16) if isinstance(high, str) else int(high)
        lo = int(low, 16) if isinstance(low, str) else int(low)
        return cls(hi >> 32, hi & MASK32, lo >> 32, lo & MASK32)

    @classmethod
    def parse(cls, text: str) -> "XorshiftState":
        """Acepta 4 palabras hex de 8 dígitos o 2 de 16 (separadas por espacios/saltos)."""
        parts = text.replace(",", " ").split()
        if len(parts) == 4:
            return cls.from_words(int(p, 16) for p in parts)
        if len(parts) == 2:
            return cls.from_hex64(parts[0], parts[1])
        raise ValueError(t("Se esperaban 4 palabras de 32 bits o 2 de 64 bits en hexadecimal",
                           "Expected 4 32-bit words or 2 64-bit words in hexadecimal"))

    @property
    def words(self) -> tuple[int, int, int, int]:
        return (self.s0, self.s1, self.s2, self.s3)

    def as_int128(self) -> int:
        """Estado como un entero de 128 bits (``s0`` en los bits altos): es el vector que usa el salto GF(2)."""
        return (self.s0 << 96) | (self.s1 << 64) | (self.s2 << 32) | self.s3

    def to_hex32(self) -> str:
        """Formato ``S[0..3]``: cuatro palabras hex separadas por espacios."""
        return " ".join(f"{w:08X}" for w in self.words)

    def to_hex64(self) -> tuple[str, str]:
        """Formato ``S[0-1]``: dos u64 en hex."""
        return (f"{self.s0:08X}{self.s1:08X}", f"{self.s2:08X}{self.s3:08X}")

    def __str__(self) -> str:  # pragma: no cover - representación
        return self.to_hex32()


class Xorshift128:
    """Generador mutable. ``next()`` devuelve la nueva ``s3``."""

    __slots__ = ("s0", "s1", "s2", "s3")

    def __init__(self, s0: int, s1: int, s2: int, s3: int) -> None:
        self.s0, self.s1, self.s2, self.s3 = s0 & MASK32, s1 & MASK32, s2 & MASK32, s3 & MASK32

    @classmethod
    def from_state(cls, state: XorshiftState) -> "Xorshift128":
        return cls(*state.words)

    @property
    def state(self) -> XorshiftState:
        return XorshiftState(self.s0, self.s1, self.s2, self.s3)

    def copy(self) -> "Xorshift128":
        return Xorshift128(self.s0, self.s1, self.s2, self.s3)

    def next(self) -> int:
        """Avanza una llamada y devuelve la salida (la nueva ``s3``)."""
        t = self.s0 ^ ((self.s0 << 11) & MASK32)
        s3 = self.s3
        self.s0, self.s1, self.s2 = self.s1, self.s2, s3
        self.s3 = t ^ (t >> 8) ^ s3 ^ (s3 >> 19)
        return self.s3

    def prev(self) -> int:
        """Deshace el último ``next()`` y devuelve el valor que ese ``next()`` había producido."""
        undone = self.s3
        # s3_new = t ^ (t >> 8) ^ s2_new ^ (s2_new >> 19), donde s2_new = s3_old.
        y = undone ^ self.s2 ^ (self.s2 >> 19)
        # Invertir y = t ^ (t >> 8)
        t = y ^ (y >> 8)
        t ^= t >> 16
        # Invertir t = s0 ^ (s0 << 11)
        s0 = t ^ ((t << 11) & MASK32)
        s0 ^= (s0 << 22) & MASK32
        self.s0, self.s1, self.s2, self.s3 = s0, self.s0, self.s1, self.s2
        return undone

    def advance(self, n: int) -> None:
        """Avanza ``n`` llamadas una a una (con ``n`` negativo, retrocede con ``prev``). Coste O(|n|)."""
        if n < 0:
            for _ in range(-n):
                self.prev()
            return
        for _ in range(n):
            self.next()

    def outputs(self, n: int) -> list[int]:
        """Las ``n`` siguientes salidas (avanza el generador)."""
        return [self.next() for _ in range(n)]

    def jump(self, n: int) -> None:
        """Avanza ``n`` llamadas en O(log n) mediante potencias de la matriz de transición GF(2)."""
        if n < 0:
            raise ValueError(t("jump solo admite n >= 0", "jump only accepts n >= 0"))
        vector = self.state.as_int128()
        bit = 0
        while n:
            if n & 1:
                vector = _apply(_transition_power(bit), vector)
            n >>= 1
            bit += 1
        self.s0, self.s1, self.s2, self.s3 = (
            (vector >> 96) & MASK32, (vector >> 64) & MASK32, (vector >> 32) & MASK32, vector & MASK32,
        )

    def skip(self, n: int) -> None:
        """Como ``advance`` (admite negativos), pero en O(log n) hacia delante."""
        if n >= 0:
            self.jump(n)
        else:
            self.advance(n)

    # --- Derivados usados por el juego -------------------------------------------------
    def rand_range(self, minimum: int, maximum: int) -> int:
        """Entero en ``[minimum, maximum)``: ``next() % (max - min) + min``."""
        return self.next() % (maximum - minimum) + minimum

    def rand_float(self, minimum: float, maximum: float) -> float:
        """Float como lo genera BDSP (interpolación invertida: con ``t = 1`` sale ``minimum``).

        ``t = (next() & 0x7FFFFF) / 8388607``; resultado ``t*min + (1-t)*max``.
        """
        t = (self.next() & FLOAT_MANTISSA_MASK) / FLOAT_DIVISOR
        return t * minimum + (1.0 - t) * maximum


# --- Salto por matrices GF(2) ------------------------------------------------------------------
# Matriz 128×128 como lista de columnas: columna j = imagen del vector unitario e_j.
_POWERS: list[list[int]] = []


def _step_vector(vector: int) -> int:
    """Aplica un ``next()`` a un estado dado como entero de 128 bits."""
    rng = Xorshift128((vector >> 96) & MASK32, (vector >> 64) & MASK32, (vector >> 32) & MASK32, vector & MASK32)
    rng.next()
    return rng.state.as_int128()


def _apply(columns: list[int], vector: int) -> int:
    """Producto matriz × vector en GF(2): XOR de las columnas cuyos bits están a 1 en ``vector``."""
    out = 0
    j = 0
    while vector:
        if vector & 1:
            out ^= columns[j]
        vector >>= 1
        j += 1
    return out


def _transition_power(k: int) -> list[int]:
    """Columnas de T^(2^k), calculadas y cacheadas bajo demanda.

    T es la transición de un ``next()``, que es lineal en GF(2); cada potencia se obtiene elevando al cuadrado la
    anterior. Con ellas ``jump`` avanza ``n`` llamadas aplicando una matriz por cada bit a 1 de ``n``.
    """
    if not _POWERS:
        _POWERS.append([_step_vector(1 << j) for j in range(128)])
    while len(_POWERS) <= k:
        prev = _POWERS[-1]
        _POWERS.append([_apply(prev, col) for col in prev])
    return _POWERS[k]


def float_from_output(value: int, minimum: float, maximum: float) -> float:
    """Mismo cálculo que ``Xorshift128.rand_float`` sobre una salida ya generada."""
    t = (value & FLOAT_MANTISSA_MASK) / FLOAT_DIVISOR
    return t * minimum + (1.0 - t) * maximum
