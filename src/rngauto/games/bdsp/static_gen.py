"""Generación de encuentros estáticos de BDSP (incluidos roamers) a partir del estado Xorshift.

Implementado a partir del comportamiento del juego y verificado en los tests con resultados de referencia
independientes.

Convención de avances: generar en el avance ``a`` significa que el juego ya ha consumido ``a``
llamadas desde el estado base y la siguiente llamada produce el EC. Los retrasos entre la última
acción y la generación (``final delay``) los modela el planificador, no este módulo (``offset = 0``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rngauto.core.pokemon import Gender, PokemonTraits, Shiny
from rngauto.i18n import t

from .xorshift import MASK32, Xorshift128, XorshiftState

MASK64 = 0xFFFFFFFFFFFFFFFF


def gen(value: int) -> int:
    """Transformación de cada salida en estáticos: ``Random.Range(int.MinValue, int.MaxValue)``.

    ``((r mod 0xFFFFFFFF) + 0x80000000) mod 2^32`` — es ``r ^ 0x80000000`` salvo ``r = 0xFFFFFFFF``.
    """
    return ((value % 0xFFFFFFFF) + 0x80000000) & MASK32


class ShinyPolicy(Enum):
    """Si el encuentro puede salir shiny o tiene bloqueo."""

    RANDOM = "random"
    NEVER = "never"  # shiny-lock (Mew, Jirachi)


class AbilityMode(Enum):
    """Cómo se decide la habilidad: ranura fija, oculta o al azar entre las dos normales."""

    SLOT_0 = 0
    SLOT_1 = 1
    HIDDEN = 2  # consume una llamada y la descarta
    RANDOM = 255


class LeadKind(Enum):
    """Habilidad del primer Pokémon del equipo que influye en la generación."""

    NONE = "none"
    SYNCHRONIZE = "synchronize"
    CUTE_CHARM = "cute_charm"


@dataclass(frozen=True, slots=True)
class Lead:
    """Líder del equipo: Sincronía fija la naturaleza y Gran Encanto sesga el género (consume una llamada)."""

    kind: LeadKind = LeadKind.NONE
    nature: int | None = None  # Sincronía
    gender: Gender | None = None  # género del líder con Gran Encanto

    def __post_init__(self) -> None:
        if self.kind is LeadKind.SYNCHRONIZE and self.nature is None:
            raise ValueError(t("Sincronía requiere la naturaleza del líder", "Synchronize requires the lead's nature"))
        if self.kind is LeadKind.CUTE_CHARM and self.gender not in (Gender.MALE, Gender.FEMALE):
            raise ValueError(t("Gran Encanto requiere el género del líder", "Cute Charm requires the lead's gender"))


NO_LEAD = Lead()


@dataclass(frozen=True, slots=True)
class StaticTemplate:
    """Datos fijos de un encuentro estático: especie, nivel, IV perfectos garantizados, género y habilidad."""

    key: str
    name: str
    species: int
    level: int
    iv_count: int
    gender_ratio: int  # 255 sin género, 254 solo ♀, 0 solo ♂, otro = umbral
    shiny: ShinyPolicy = ShinyPolicy.RANDOM
    ability: AbilityMode = AbilityMode.RANDOM
    fateful: bool = False
    roamer: bool = False
    form: int = 0
    version: str = "BDSP"
    category: str = ""
    base_stats: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)  # HP, Atk, Def, SpA, SpD, Spe


def shiny_type(pid: int, tsv: int) -> Shiny:
    """Tipo de shiny de ``pid`` con el TSV dado: XOR 0 es cuadrado, menor que 16 es estrella."""
    x = (pid >> 16) ^ (pid & 0xFFFF) ^ tsv
    if x == 0:
        return Shiny.SQUARE
    if x < 16:
        return Shiny.STAR
    return Shiny.NONE


def _apply_shiny(pid: int, fake: int, tsv: int, policy: ShinyPolicy, fateful: bool) -> tuple[int, Shiny]:
    """Decisión shiny con el TID/SID ficticio y ajuste del PID al TSV real.

    El juego decide si brilla con un TSV ficticio (``fake``) y luego reescribe el PID para que, con el TSV real
    del jugador, dé el mismo resultado. Devuelve ``(pid ajustado, tipo de shiny)``.
    """
    if policy is ShinyPolicy.NEVER:
        if shiny_type(pid, tsv) is not Shiny.NONE:
            pid ^= 0x10000000  # cambiar un bit alto basta para que deje de ser shiny con el TSV real
        return pid, Shiny.NONE
    fake_tsv = (fake >> 16) ^ (fake & 0xFFFF)
    shiny = shiny_type(pid, fake_tsv)
    if shiny is not Shiny.NONE:
        if fateful:
            shiny = Shiny.SQUARE
        if shiny_type(pid, tsv) is not shiny:
            # Mitad alta = baja ^ TSV ^ (0 cuadrado, 1 estrella): fuerza el mismo tipo con el TSV real.
            low = pid & 0xFFFF
            pid = (((low ^ tsv ^ (2 - int(shiny))) & 0xFFFF) << 16) | low
    elif shiny_type(pid, tsv) is not Shiny.NONE:
        pid ^= 0x10000000
    return pid, shiny


class GenStream:
    """Flujo perezoso de ``gen(next())`` desde un estado; índice 0 = primera llamada."""

    __slots__ = ("_rng", "values")

    def __init__(self, state: XorshiftState) -> None:
        self._rng = Xorshift128.from_state(state)
        self.values: list[int] = []

    def ensure(self, length: int) -> None:
        """Genera salidas hasta tener al menos ``length``."""
        values, rng = self.values, self._rng
        while len(values) < length:
            values.append(gen(rng.next()))

    def __getitem__(self, index: int) -> int:
        if index >= len(self.values):
            self.ensure(index + 64)  # por bloques, para no generar de una en una
        return self.values[index]


def generate_static(stream: GenStream, start: int, template: StaticTemplate, tsv: int = 0, lead: Lead = NO_LEAD) -> PokemonTraits:
    """Genera el Pokémon cuyo EC es la salida ``start`` del flujo.

    Orden de consumo: EC, TID/SID ficticio, PID, posiciones de los IV perfectos (repitiendo si cae en una ya
    fijada), resto de IV, habilidad, género (con Gran Encanto, una llamada antes), naturaleza (salvo Sincronía),
    altura y peso. Los roamers solo usan aquí el EC: el resto sale de su propio generador.
    """
    if template.roamer:
        return _generate_roamer(stream[start], template, tsv, lead)
    p = start
    ec = stream[p]
    fake = stream[p + 1]
    pid = stream[p + 2]
    p += 3
    pid, shiny = _apply_shiny(pid, fake, tsv, template.shiny, template.fateful)

    ivs: list[int | None] = [None] * 6
    fixed = 0
    while fixed < template.iv_count:
        k = stream[p] % 6
        p += 1
        if ivs[k] is None:
            ivs[k] = 31
            fixed += 1
    for k in range(6):
        if ivs[k] is None:
            ivs[k] = stream[p] % 32
            p += 1

    if template.ability is AbilityMode.RANDOM:
        ability = stream[p] % 2
        p += 1
    elif template.ability is AbilityMode.HIDDEN:
        ability = 2
        p += 1  # el juego consume una llamada que se descarta
    else:
        ability = template.ability.value

    ratio = template.gender_ratio
    if ratio == 255:
        gender = Gender.GENDERLESS
    elif ratio == 254:
        gender = Gender.FEMALE
    elif ratio == 0:
        gender = Gender.MALE
    else:
        gender = None
        if lead.kind is LeadKind.CUTE_CHARM:
            roll = stream[p] % 3
            p += 1
            if roll > 0:
                gender = Gender.MALE if lead.gender is Gender.FEMALE else Gender.FEMALE
        if gender is None:
            gender = Gender.FEMALE if (stream[p] % 253) + 1 < ratio else Gender.MALE
            p += 1

    if lead.kind is LeadKind.SYNCHRONIZE:
        nature = lead.nature
    else:
        nature = stream[p] % 25
        p += 1

    height = stream[p] % 129 + stream[p + 1] % 128
    weight = stream[p + 2] % 129 + stream[p + 3] % 128
    return PokemonTraits(
        species=template.species, level=template.level, ec=ec, pid=pid, shiny=shiny,
        ivs=tuple(ivs), ability_slot=ability, gender=gender, nature=nature,
        height=height, weight=weight, form=template.form,
    )


# --- Roamers (Mesprit, Cresselia) ---------------------------------------------------------------

def _splitmix(seed: int) -> int:
    """Mezcla SplitMix64 con la que se siembran las dos palabras del Xoroshiro."""
    seed = (0xBF58476D1CE4E5B9 * (seed ^ (seed >> 30))) & MASK64
    seed = (0x94D049BB133111EB * (seed ^ (seed >> 27))) & MASK64
    return seed ^ (seed >> 31)


def _rotl64(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & MASK64


class XoroshiroBDSP:
    """Xoroshiro128+ sembrado como en BDSP (roamers, huevos)."""

    __slots__ = ("s0", "s1")

    def __init__(self, seed: int) -> None:
        self.s0 = _splitmix((seed + 0x9E3779B97F4A7C15) & MASK64)
        self.s1 = _splitmix((seed + 0x3C6EF372FE94F82A) & MASK64)

    def next(self) -> int:
        s0, s1 = self.s0, self.s1
        result = (s0 + s1) & MASK64
        s1 ^= s0
        self.s0 = _rotl64(s0, 24) ^ s1 ^ ((s1 << 16) & MASK64)
        self.s1 = _rotl64(s1, 37)
        return result

    def next_uint(self, maximum: int) -> int:
        """Entero en ``[0, maximum)`` a partir de los 32 bits altos de la salida."""
        return (self.next() >> 32) % maximum


def _generate_roamer(ec: int, template: StaticTemplate, tsv: int, lead: Lead) -> PokemonTraits:
    """Genera un roamer: el EC sale del Xorshift y siembra un Xoroshiro del que sale todo lo demás.

    Siempre tres IV perfectos y nunca consume llamada para Gran Encanto; la habilidad se tira igualmente.
    """
    rng = XoroshiroBDSP(ec)  # EC extendido con ceros (≠ huevos, que extienden signo)
    fake = rng.next_uint(0xFFFFFFFF)
    pid = rng.next_uint(0xFFFFFFFF)
    pid, shiny = _apply_shiny(pid, fake, tsv, ShinyPolicy.RANDOM, fateful=False)
    ivs: list[int | None] = [None] * 6
    fixed = 0
    while fixed < 3:
        k = rng.next_uint(6)
        if ivs[k] is None:
            ivs[k] = 31
            fixed += 1
    for k in range(6):
        if ivs[k] is None:
            ivs[k] = rng.next_uint(32)
    ability = rng.next_uint(2)
    gender = Gender.FEMALE if template.species == 488 else Gender.GENDERLESS  # 488 = Cresselia (siempre ♀)
    nature = lead.nature if lead.kind is LeadKind.SYNCHRONIZE else rng.next_uint(25)
    height = rng.next_uint(129) + rng.next_uint(128)
    weight = rng.next_uint(129) + rng.next_uint(128)
    return PokemonTraits(
        species=template.species, level=template.level, ec=ec, pid=pid, shiny=shiny,
        ivs=tuple(ivs), ability_slot=ability, gender=gender, nature=nature,
        height=height, weight=weight, form=template.form,
    )
