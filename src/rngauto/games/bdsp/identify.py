"""Del Pokémon observado al advance obtenido: base de la calibración (esperado vs obtenido).

Se generan los vecinos del objetivo y se quedan los compatibles con lo observado. Como con pocas
observaciones (p. ej. solo naturaleza) puede haber varios candidatos, el resultado es una lista: la
calibración decide (historial incierto/confirmado). También sirve para elegir objetivos
"calibrables": aquellos cuyos vecinos se distinguen con lo que podemos observar.
"""
from __future__ import annotations

from dataclasses import dataclass

from rngauto.core.pokemon import Gender, IVRange, PokemonTraits, Shiny

from .encounters import StaticTargetFinder
from .xorshift import XorshiftState


@dataclass(frozen=True, slots=True)
class Observation:
    """Lo que la verificación pudo leer. ``None`` = no observado."""

    shiny: Shiny | None = None
    is_shiny: bool | None = None  # cuando solo se sabe si brilla, no el tipo
    nature: int | None = None
    gender: Gender | None = None
    ability_slot: int | None = None
    ivs: tuple[IVRange, ...] | None = None  # rangos deducidos de stats o del juez
    natures: frozenset[int] | None = None  # varias posibles (p. ej. naturaleza neutra: sin colores en la ficha)

    def compatible(self, p: PokemonTraits) -> bool:
        """¿Encaja ``p`` con todo lo observado? Los campos ``None`` no restringen."""
        if self.shiny is not None and p.shiny != self.shiny:
            return False
        if self.is_shiny is not None and (p.shiny != Shiny.NONE) != self.is_shiny:
            return False
        if self.nature is not None and p.nature != self.nature:
            return False
        if self.natures is not None and p.nature not in self.natures:
            return False
        if self.gender is not None and p.gender != self.gender:
            return False
        if self.ability_slot is not None and p.ability_slot != self.ability_slot:
            return False
        if self.ivs is not None and not all(v in r for v, r in zip(p.ivs, self.ivs)):
            return False
        return True

    def fields(self) -> list[str]:
        """Nombres de los campos observados (los que no son ``None``)."""
        return [name for name in ("shiny", "is_shiny", "nature", "natures", "gender", "ability_slot", "ivs")
                if getattr(self, name) is not None]


def observe_like(p: PokemonTraits, fields: list[str]) -> Observation:
    """Observación que haríamos de ``p`` si solo viéramos ``fields`` (para planificar)."""
    kwargs = {}
    for f in fields:
        if f == "is_shiny":
            kwargs[f] = p.shiny != Shiny.NONE
        elif f == "ivs":
            kwargs[f] = tuple(IVRange(v, v) for v in p.ivs)
        else:
            kwargs[f] = getattr(p, f)
    return Observation(**kwargs)


def candidate_advances(
    finder: StaticTargetFinder,
    base: XorshiftState,
    target: int,
    observation: Observation,
    radius: int = 50,
) -> list[int]:
    """Advances en ``target ± radius`` compatibles con la observación, ordenados por cercanía."""
    found = [t.advance for t in finder.neighbourhood(base, target, radius) if observation.compatible(t.traits)]
    return sorted(found, key=lambda a: (abs(a - target), a))


def ambiguity(
    finder: StaticTargetFinder,
    base: XorshiftState,
    target: int,
    fields: list[str],
    radius: int = 5,
) -> int:
    """Cuántos advances de ``target ± radius`` serían indistinguibles del real si fallamos por poco.

    Para cada posible resultado ``a`` en el vecindario, cuenta cuántos otros generan la misma
    observación; devuelve el peor caso (1 = siempre identificable).
    """
    neigh = finder.neighbourhood(base, target, radius)
    worst = 1
    for t in neigh:
        obs = observe_like(t.traits, fields)
        same = sum(1 for u in neigh if obs.compatible(u.traits))
        worst = max(worst, same)
    return worst
