"""Catálogo de plantillas estáticas de BDSP y buscador de objetivos.

Las plantillas se leen de ``data/static_encounters.json``. ``StaticTargetFinder`` recorre una ventana de
advances con :func:`static_gen.generate_static` para encontrar los que cumplen unos criterios.
"""
from __future__ import annotations

import json
from dataclasses import replace
from functools import lru_cache
from importlib import resources

from rngauto.core.pokemon import Gender, PokemonTraits, TargetCriteria
from rngauto.core.target import Target
from rngauto.i18n import t

from .static_gen import NO_LEAD, AbilityMode, GenStream, Lead, ShinyPolicy, StaticTemplate, generate_static
from .xorshift import Xorshift128, XorshiftState


@lru_cache(maxsize=1)
def static_templates() -> dict[str, StaticTemplate]:
    """Todas las plantillas del catálogo por clave. Se lee una sola vez."""
    raw = json.loads(resources.files(__package__).joinpath("data/static_encounters.json").read_text("utf-8"))
    out: dict[str, StaticTemplate] = {}
    for tpl in raw["templates"]:
        out[tpl["key"]] = StaticTemplate(
            key=tpl["key"], name=tpl["name"], species=tpl["species"], level=tpl["level"],
            iv_count=tpl["iv_count"], gender_ratio=tpl["gender_ratio"],
            shiny=ShinyPolicy(tpl["shiny"]), ability=AbilityMode(tpl["ability"]),
            fateful=tpl["fateful"], roamer=tpl["roamer"], form=tpl["form"],
            version=tpl["version"], category=tpl["category"],
            base_stats=tuple(tpl.get("base_stats", (0,) * 6)),
        )
    return out


def static_template(key: str) -> StaticTemplate:
    """Plantilla de clave ``key``; ``KeyError`` con mensaje traducido si no existe."""
    try:
        return static_templates()[key]
    except KeyError:
        raise KeyError(t("Plantilla estática desconocida: {key!r}", "Unknown static template: {key!r}", key=key)) from None


def encounter_species(encounter) -> list[StaticTemplate]:
    """Plantillas de las especies que pueden salir en el encuentro (p. ej. los tres Regis de la Losa Inicio,
    o Latias/Latios de la Losa Empatía). Sin lista: solo la plantilla del encuentro."""
    return [static_template(k) for k in encounter.species] or [static_template(encounter.template)]


def target_name(encounter) -> str:
    """Nombre del objetivo: «Latias/Latios» si sale uno al azar, porque no se sabe cuál saldrá."""
    return "/".join(t.name for t in encounter_species(encounter))


def target_label(encounter, traits: PokemonTraits) -> str:
    """«Latias/Latios · ★ shiny · …». Si las especies posibles tienen distinto género fijo (♀ Latias, ♂ Latios),
    el género no se puede saber de antemano y no se muestra (no consume RNG: la generación es la misma)."""
    if len({t.gender_ratio for t in encounter_species(encounter)}) > 1:
        traits = replace(traits, gender=Gender.GENDERLESS)
    return f"{target_name(encounter)} · {traits.describe()}"


class StaticTargetFinder:
    """TargetFinder para estáticos/roamers de BDSP.

    ``tid``/``sid`` solo afectan al PID final (la shinyness la decide el TID/SID ficticio), por lo
    que pueden dejarse a 0 si no se conocen.
    """

    def __init__(self, template: StaticTemplate, tid: int = 0, sid: int = 0, lead: Lead = NO_LEAD) -> None:
        if not (0 <= tid <= 0xFFFF and 0 <= sid <= 0xFFFF):
            raise ValueError(t("TID/SID son de 16 bits (0..65535)", "TID/SID are 16-bit values (0..65535)"))
        self.template = template
        self.tsv = tid ^ sid
        self.lead = lead

    def generate(self, state: XorshiftState, advance: int) -> PokemonTraits:
        """Pokémon que saldría en el advance ``advance`` contando desde ``state``."""
        rng = Xorshift128.from_state(state)
        rng.skip(advance)
        return generate_static(GenStream(rng.state), 0, self.template, self.tsv, self.lead)

    def search(
        self,
        state: XorshiftState,
        criteria: TargetCriteria,
        min_advance: int,
        max_advance: int,
        limit: int = 100,
    ) -> list[Target]:
        """Advances de ``[min_advance, max_advance]`` cuyo Pokémon cumple ``criteria``, en orden y como mucho
        ``limit``."""
        if max_advance < min_advance:
            raise ValueError("max_advance < min_advance")
        rng = Xorshift128.from_state(state)
        rng.skip(min_advance)
        stream = GenStream(rng.state)
        # Un Pokémon consume unas 20 salidas a partir de su advance: se genera la ventana con margen de una vez.
        stream.ensure(max_advance - min_advance + 64)
        found: list[Target] = []
        for i in range(max_advance - min_advance + 1):
            traits = generate_static(stream, i, self.template, self.tsv, self.lead)
            if criteria.matches(traits):
                found.append(Target(min_advance + i, traits))
                if len(found) >= limit:
                    break
        return found

    def neighbourhood(self, state: XorshiftState, center: int, radius: int) -> list[Target]:
        """Resultados alrededor de un advance: base para identificar qué advance se obtuvo."""
        lo = max(0, center - radius)
        rng = Xorshift128.from_state(state)
        rng.skip(lo)
        stream = GenStream(rng.state)
        return [
            Target(lo + i, generate_static(stream, i, self.template, self.tsv, self.lead))
            for i in range(center + radius - lo + 1)
        ]
