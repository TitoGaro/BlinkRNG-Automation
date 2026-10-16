"""Generador de estáticos de BDSP.

Los vectores de PokeFinder (GPL-3.0) NO se copian al repositorio: se leen como oráculo externo desde
la instalación local (variable ``POKEFINDER_DIR`` o ``../PokeFinder`` junto al proyecto). Si no están,
esos tests se omiten.
"""
import json
import os
from pathlib import Path

import pytest

from rngauto.core.pokemon import Gender, Shiny, ShinyFilter, TargetCriteria
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template, static_templates
from rngauto.games.bdsp.static_gen import Lead, LeadKind, XoroshiroBDSP, gen
from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState

PROJECT = Path(__file__).resolve().parents[2]
POKEFINDER = Path(os.environ.get("POKEFINDER_DIR", PROJECT.parent / "PokeFinder"))
STATIC_JSON = POKEFINDER / "Test" / "Gen8" / "static8.json"
XORSHIFT_JSON = POKEFINDER / "Test" / "RNG" / "xorshift.json"
needs_oracle = pytest.mark.skipif(not STATIC_JSON.exists(), reason="PokeFinder local no disponible")

CATEGORY_ORDER = (
    "starter", "gift", "fossil", "stationary", "roamer", "legend",
    "ramanas_pure", "ramanas_strange", "mythic",
)
TID, SID = 12345, 54321  # perfil de los tests de PokeFinder


def template_by_index(category: int, index: int):
    cat = CATEGORY_ORDER[category]
    return [t for t in static_templates().values() if t.category == cat][index]


def lead_from(name: str) -> Lead:
    if name == "None":
        return Lead()
    if name == "Synchronize":
        return Lead(LeadKind.SYNCHRONIZE, nature=0)
    if name == "CuteCharmF":
        return Lead(LeadKind.CUTE_CHARM, gender=Gender.FEMALE)
    if name == "CuteCharmM":
        return Lead(LeadKind.CUTE_CHARM, gender=Gender.MALE)
    raise ValueError(name)


def test_gen_transform():
    assert gen(0xA20345D0) == 0x220345D0
    assert gen(0x0FD266FA) == 0x8FD266FA
    assert gen(0xFFFFFFFF) == 0x80000000


def test_catalog_complete():
    templates = static_templates()
    assert len(templates) == 47
    assert static_template("arceus").fateful and static_template("arceus").level == 80
    assert static_template("cresselia").roamer
    assert static_template("dialga").version == "BD"


@needs_oracle
@pytest.mark.parametrize("section", ["generate", "generateRoamer"])
def test_matches_pokefinder_vectors(section):
    data = json.loads(STATIC_JSON.read_text("utf-8"))[section]
    checked = 0
    for case in data:
        template = template_by_index(case["category"], case["pokemon"])
        state = XorshiftState.from_hex64(case["seed0"], case["seed1"])
        finder = StaticTargetFinder(template, TID, SID, lead_from(case["lead"]))
        for expected in case["results"]:
            got = finder.generate(state, expected["advances"])
            assert got.ec == expected["ec"], case["name"]
            assert got.pid == expected["pid"], case["name"]
            assert list(got.ivs) == expected["ivs"], case["name"]
            assert got.ability_slot == expected["ability"], case["name"]
            assert int(got.gender) == expected["gender"], case["name"]
            assert got.nature == expected["nature"], case["name"]
            assert int(got.shiny) == expected["shiny"], case["name"]
            assert (got.height, got.weight) == (expected["height"], expected["weight"]), case["name"]
            assert got.level == expected["level"]
            checked += 1
    assert checked > 0


@pytest.mark.skipif(not XORSHIFT_JSON.exists(), reason="PokeFinder local no disponible")
def test_xorshift_and_jump_match_pokefinder_vectors():
    data = json.loads(XORSHIFT_JSON.read_text("utf-8"))
    for kind, cases in data.items():
        for case in cases:
            seed = case["seed"]
            rng = Xorshift128.from_state(XorshiftState.from_hex64(seed, seed ^ (seed >> 32)))
            n = case.get("advances", 1)
            if kind == "jump":
                rng.jump(n - 1)
            elif kind == "advance":
                rng.advance(n - 1)
            assert rng.next() == case["result"], (kind, case)


def test_jump_equals_advance():
    a = Xorshift128(0x11111111, 0x22222222, 0x33333333, 0x44444444)
    b = a.copy()
    a.advance(12345)
    b.jump(12345)
    assert a.state == b.state


# Vectores shiny obtenidos por un prototipo independiente (traducción del código de PokeFinder usada
# solo para validar, fuera del repo). Dos implementaciones distintas coinciden; queda pendiente
# confirmarlos en la GUI de PokeFinder (perfil BD, TID 12345, SID 54321, mismas semillas).
SEEDS = XorshiftState.from_hex64(0x1234567887654321, 0x8765432112345678)


@pytest.mark.parametrize(
    "key,advance,ec,pid,shiny,nature,ivs",
    [
        ("dialga", 810, 0x1F75570A, 0x4505A10C, Shiny.STAR, 11, (31, 23, 10, 26, 31, 31)),
        ("dialga", 1595, 0x11D32EF8, 0xFD881981, Shiny.STAR, 24, (21, 27, 31, 1, 31, 31)),
        ("darkrai", 810, 0x1F75570A, 0x4504A10C, Shiny.SQUARE, 11, (31, 23, 10, 26, 31, 31)),
        ("cresselia", 7437, 0x7C1A9E5D, 0x7D909999, Shiny.STAR, 22, (31, 31, 19, 31, 20, 7)),
    ],
)
def test_shiny_cross_check(key, advance, ec, pid, shiny, nature, ivs):
    got = StaticTargetFinder(static_template(key), TID, SID).generate(SEEDS, advance)
    assert (got.ec, got.pid, got.shiny, got.nature, got.ivs) == (ec, pid, shiny, nature, ivs)


def test_search_finds_shiny_targets():
    finder = StaticTargetFinder(static_template("dialga"), TID, SID)
    targets = finder.search(SEEDS, TargetCriteria(shiny=ShinyFilter.SHINY), 0, 2000)
    advances = [t.advance for t in targets]
    assert 810 in advances and 1595 in advances
    for t in targets:
        assert t.traits.shiny is not Shiny.NONE
        assert finder.generate(SEEDS, t.advance) == t.traits


def test_shiny_independent_of_trainer_id():
    """La shinyness depende del TID/SID ficticio: cambiar el TID solo cambia el PID."""
    a = StaticTargetFinder(static_template("dialga"), 1, 2).generate(SEEDS, 810)
    b = StaticTargetFinder(static_template("dialga"), 40000, 999).generate(SEEDS, 810)
    assert a.shiny == b.shiny == Shiny.STAR
    assert a.ivs == b.ivs and a.nature == b.nature and a.ec == b.ec


def test_xoroshiro_bdsp_deterministic():
    r1, r2 = XoroshiroBDSP(0x1234), XoroshiroBDSP(0x1234)
    assert [r1.next() for _ in range(5)] == [r2.next() for _ in range(5)]
