"""Target Generation (independiente del juego): qué advance produce el Pokémon deseado.

El TargetFinder no sabe nada de botones ni de visión: recibe un estado RNG, un encuentro y unos
criterios, y devuelve advances candidatos. Cómo llegar a ellos es cosa del AdvancePlanner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .pokemon import PokemonTraits, TargetCriteria

StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class Target:
    """Advance en el que se genera un Pokémon que cumple los criterios."""

    advance: int  # llamadas consumidas desde el estado base cuando el juego genera el Pokémon
    traits: PokemonTraits

    def describe(self) -> str:
        return f"advance {self.advance}: {self.traits.describe()}"


class TargetFinder(Protocol, Generic[StateT]):
    """Contrato común; cada juego/método aporta su implementación."""

    def generate(self, state: StateT, advance: int) -> PokemonTraits:
        """Pokémon que se generaría tras consumir ``advance`` llamadas desde ``state``."""
        ...

    def search(
        self,
        state: StateT,
        criteria: TargetCriteria,
        min_advance: int,
        max_advance: int,
        limit: int = 100,
    ) -> list[Target]:
        """Hasta ``limit`` objetivos en ``[min_advance, max_advance]`` que cumplen ``criteria``, en orden."""
        ...
