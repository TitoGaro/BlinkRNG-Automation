"""Álgebra lineal sobre GF(2) con enteros de Python como vectores de bits.

Independiente del juego: sirve para cualquier RNG lineal (Xorshift, Xoroshiro, LFSR...).
Una ecuación es ``(mask, rhs)``: la paridad de las variables con bit a 1 en ``mask`` vale ``rhs``.

En lugar de montar una matriz y aplicar Gauss-Jordan al final, se mantiene una base XOR incremental: cada
ecuación se reduce al llegar y se cuentan las redundantes y las contradicciones, necesarias para dar una
confianza y para detectar observaciones erróneas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rngauto.i18n import t


@dataclass(slots=True)
class LinearSystem:
    """Sistema incremental ``A·x = b`` sobre GF(2) con ``n_vars`` incógnitas."""

    n_vars: int
    # Base en escalón: pivote (bit más alto de la fila) → (máscara, término independiente).
    _basis: dict[int, tuple[int, int]] = field(default_factory=dict)
    redundant: int = 0
    conflicts: int = 0

    def add(self, mask: int, rhs: int) -> str:
        """Añade una ecuación. Devuelve ``"new"``, ``"redundant"`` o ``"conflict"``."""
        rhs &= 1
        # Reducción por la base: si la ecuación se anula del todo, era combinación de las anteriores; con
        # término independiente 1 las contradice (alguna observación es errónea).
        while mask:
            pivot = mask.bit_length() - 1
            row = self._basis.get(pivot)
            if row is None:
                self._basis[pivot] = (mask, rhs)
                return "new"
            mask ^= row[0]
            rhs ^= row[1]
        if rhs:
            self.conflicts += 1
            return "conflict"
        self.redundant += 1
        return "redundant"

    @property
    def rank(self) -> int:
        """Ecuaciones independientes acumuladas."""
        return len(self._basis)

    @property
    def is_determined(self) -> bool:
        """``True`` si el sistema tiene solución única."""
        return self.rank == self.n_vars

    @property
    def free_variables(self) -> list[int]:
        """Variables no determinadas (dimensión del espacio nulo = ``len(free_variables)``)."""
        return [i for i in range(self.n_vars) if i not in self._basis]

    def solve(self) -> int | None:
        """Solución única como entero de ``n_vars`` bits, o ``None`` si el rango es insuficiente."""
        if not self.is_determined:
            return None
        return self._solve_with(0)

    def _solve_with(self, free_assignment: int) -> int:
        """Solución con las variables libres fijadas a los bits de ``free_assignment``."""
        solution = free_assignment
        # Cada fila tiene pivote = su bit más alto; resolviendo de menor a mayor pivote, los bits
        # inferiores (pivotes o libres) ya son conocidos.
        for pivot in sorted(self._basis):
            mask, rhs = self._basis[pivot]
            lower = mask & ~(1 << pivot)
            bit = rhs ^ parity(lower & solution)
            solution |= bit << pivot
        return solution

    def solutions(self, max_free: int = 12):
        """Genera todas las soluciones si el espacio nulo tiene como mucho ``max_free`` dimensiones.

        Recorre el espacio nulo asignando todas las combinaciones a las variables libres; las soluciones se
        filtran después con observaciones no lineales (tiradas sin parpadeo).
        """
        free = self.free_variables
        if len(free) > max_free:
            raise ValueError(t("Espacio nulo demasiado grande: {n} > {max_free} bits libres",
                               "Null space too large: {n} > {max_free} free bits", n=len(free), max_free=max_free))
        for combo in range(1 << len(free)):
            assignment = 0
            for j, var in enumerate(free):
                if (combo >> j) & 1:
                    assignment |= 1 << var
            yield self._solve_with(assignment)


def parity(value: int) -> int:
    """Paridad (XOR de todos los bits) de ``value``."""
    return value.bit_count() & 1


class SymbolicWord:
    """Palabra de 32 bits cuyos bits son combinaciones lineales (máscaras) de las incógnitas.

    Permite ejecutar el RNG «en simbólico»: XOR y desplazamientos son lineales en GF(2), así que cada bit de
    salida queda expresado como una máscara sobre los bits de la semilla y se convierte en una ecuación."""

    __slots__ = ("bits",)
    WIDTH = 32

    def __init__(self, bits: list[int]) -> None:
        # ``bits[j]``: máscara del bit ``j`` (0 = menos significativo).
        self.bits = bits

    @classmethod
    def variable(cls, word_index: int, words: int = 4) -> "SymbolicWord":
        """Palabra ``word_index`` del estado inicial. La variable ``i`` es el bit ``i`` del entero
        de 128 bits ``(s0 << 96) | (s1 << 64) | (s2 << 32) | s3``."""
        base = (words - 1 - word_index) * cls.WIDTH
        return cls([1 << (base + j) for j in range(cls.WIDTH)])

    def __xor__(self, other: "SymbolicWord") -> "SymbolicWord":
        return SymbolicWord([a ^ b for a, b in zip(self.bits, other.bits)])

    def shl(self, k: int) -> "SymbolicWord":
        """Desplazamiento a la izquierda de 32 bits (los bits altos se pierden)."""
        return SymbolicWord([0] * k + self.bits[: self.WIDTH - k])

    def shr(self, k: int) -> "SymbolicWord":
        """Desplazamiento lógico a la derecha."""
        return SymbolicWord(self.bits[k:] + [0] * k)

    def bit(self, j: int) -> int:
        return self.bits[j]

    def evaluate(self, assignment: int) -> int:
        """Valor concreto de la palabra dada una asignación de las incógnitas."""
        return sum(parity(mask & assignment) << j for j, mask in enumerate(self.bits))
