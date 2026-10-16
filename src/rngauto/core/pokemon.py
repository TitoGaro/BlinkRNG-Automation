"""Conceptos de Pokémon independientes del juego (resultado de generación, naturalezas, filtros)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from rngauto.i18n import is_spanish, t

NATURES_EN = (
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty", "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive", "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
)
NATURES_ES = (
    "Fuerte", "Huraña", "Audaz", "Firme", "Pícara", "Osada", "Dócil", "Plácida", "Agitada", "Floja",
    "Miedosa", "Activa", "Seria", "Alegre", "Ingenua", "Modesta", "Afable", "Mansa", "Tímida", "Alocada",
    "Serena", "Amable", "Grosera", "Cauta", "Rara",
)
STAT_NAMES = ("HP", "Atk", "Def", "SpA", "SpD", "Spe")
STAT_NAMES_ES = ("PS", "Ata", "Def", "AtE", "DfE", "Vel")


def nature_name(index: int) -> str:
    """Nombre de la naturaleza en el idioma actual (solo para mostrar; se guarda y se compara por índice)."""
    return (NATURES_ES if is_spanish() else NATURES_EN)[index]


STAT_LABELS_ES = ("PS", "Ataque", "Defensa", "At. Esp.", "Def. Esp.", "Velocidad")
STAT_LABELS_EN = ("HP", "Attack", "Defense", "Sp. Atk", "Sp. Def", "Speed")


def stat_name(index: int) -> str:
    """Abreviatura del stat en el idioma actual (solo para mostrar)."""
    return (STAT_NAMES_ES if is_spanish() else STAT_NAMES)[index]


def stat_labels() -> tuple[str, ...]:
    """Nombres completos de las seis estadísticas en el idioma actual."""
    return STAT_LABELS_ES if is_spanish() else STAT_LABELS_EN


class Shiny(IntEnum):
    """Tipo de shiny; estrella y cuadrado solo se diferencian en la animación de entrada."""

    NONE = 0
    STAR = 1
    SQUARE = 2


class Gender(IntEnum):
    """Sexo del Pokémon generado."""

    MALE = 0
    FEMALE = 1
    GENDERLESS = 2


def nature_index(name: str) -> int:
    """Admite nombre inglés o español (sin distinguir mayúsculas)."""
    key = name.strip().lower()
    for table in (NATURES_EN, NATURES_ES):
        for i, n in enumerate(table):
            if n.lower() == key:
                return i
    raise ValueError(t("Naturaleza desconocida: {name!r}", "Unknown nature: {name!r}", name=name))


@dataclass(frozen=True, slots=True)
class PokemonTraits:
    """Lo que un generador produce y lo que la verificación puede observar."""

    species: int
    level: int
    ec: int
    pid: int
    shiny: Shiny
    ivs: tuple[int, int, int, int, int, int]
    ability_slot: int  # 0, 1 o 2 (oculta)
    gender: Gender
    nature: int
    height: int
    weight: int
    form: int = 0

    def describe(self) -> str:
        """Resumen legible de una línea para la interfaz y el registro."""
        shiny = {Shiny.NONE: "", Shiny.STAR: "★ shiny · ", Shiny.SQUARE: "■ shiny · "}[self.shiny]
        ivs = "/".join(str(v) for v in self.ivs)
        if self.ability_slot == 2:
            ability = t("habilidad oculta", "hidden ability")
        else:
            ability = t("habilidad {n}", "ability {n}", n=self.ability_slot + 1)
        gender = {Gender.MALE: " · ♂", Gender.FEMALE: " · ♀", Gender.GENDERLESS: ""}[self.gender]
        return f"{shiny}{nature_name(self.nature)} · IVs {ivs} · {ability}{gender}"


@dataclass(frozen=True, slots=True)
class IVRange:
    """Rango cerrado de IV aceptado para una estadística (``0-31`` = cualquiera)."""

    lo: int = 0
    hi: int = 31

    def __post_init__(self) -> None:
        if not 0 <= self.lo <= self.hi <= 31:
            raise ValueError(t("Rango de IV inválido: {lo}-{hi}", "Invalid IV range: {lo}-{hi}", lo=self.lo, hi=self.hi))

    def __contains__(self, value: int) -> bool:
        return self.lo <= value <= self.hi


class ShinyFilter(IntEnum):
    """Condición de shiny del objetivo."""

    ANY = 0
    SHINY = 1  # estrella o cuadrado
    STAR = 2
    SQUARE = 3
    NOT_SHINY = 4

    def accepts(self, shiny: Shiny) -> bool:
        if self is ShinyFilter.ANY:
            return True
        if self is ShinyFilter.SHINY:
            return shiny != Shiny.NONE
        if self is ShinyFilter.STAR:
            return shiny == Shiny.STAR
        if self is ShinyFilter.SQUARE:
            return shiny == Shiny.SQUARE
        return shiny == Shiny.NONE


@dataclass(frozen=True, slots=True)
class TargetCriteria:
    """Qué Pokémon queremos. ``None`` = cualquiera."""

    shiny: ShinyFilter = ShinyFilter.ANY
    natures: frozenset[int] | None = None
    ivs: tuple[IVRange, ...] = field(default_factory=lambda: tuple(IVRange() for _ in range(6)))
    gender: Gender | None = None
    ability_slot: int | None = None
    height: tuple[int, int] = (0, 255)
    weight: tuple[int, int] = (0, 255)

    def __post_init__(self) -> None:
        if len(self.ivs) != 6:
            raise ValueError(t("Se necesitan 6 rangos de IV", "Six IV ranges are required"))

    def matches(self, p: PokemonTraits) -> bool:
        """``True`` si ``p`` cumple todos los filtros."""
        if not self.shiny.accepts(p.shiny):
            return False
        if self.natures is not None and p.nature not in self.natures:
            return False
        if self.gender is not None and p.gender != self.gender:
            return False
        if self.ability_slot is not None and p.ability_slot != self.ability_slot:
            return False
        if not (self.height[0] <= p.height <= self.height[1] and self.weight[0] <= p.weight <= self.weight[1]):
            return False
        return all(v in r for v, r in zip(p.ivs, self.ivs))

    def describe(self) -> str:
        """Resumen legible de los filtros activos (solo los que restringen algo)."""
        parts = []
        shiny = {ShinyFilter.SHINY: "shiny", ShinyFilter.STAR: t("shiny estrella", "star shiny"),
                 ShinyFilter.SQUARE: t("shiny cuadrado", "square shiny"), ShinyFilter.NOT_SHINY: t("no shiny", "not shiny")}
        if self.shiny is not ShinyFilter.ANY:
            parts.append(shiny[self.shiny])
        if self.natures is not None:
            parts.append(", ".join(nature_name(n) for n in sorted(self.natures)))
        ivs = [f"{stat_name(i)} {'≥' + str(r.lo) if r.hi == 31 else f'{r.lo}-{r.hi}'}"
               for i, r in enumerate(self.ivs) if (r.lo, r.hi) != (0, 31)]
        if ivs:
            parts.append("IVs " + " ".join(ivs))
        if self.gender is not None:
            parts.append({Gender.MALE: t("macho", "male"), Gender.FEMALE: t("hembra", "female"),
                          Gender.GENDERLESS: t("sin género", "genderless")}[self.gender])
        if self.ability_slot == 2:
            parts.append(t("habilidad oculta", "hidden ability"))
        elif self.ability_slot is not None:
            parts.append(t("habilidad {n}", "ability {n}", n=self.ability_slot + 1))
        return " · ".join(parts) or t("cualquiera", "any")
