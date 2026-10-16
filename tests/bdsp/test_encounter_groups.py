"""Encuentros en los que sale una especie al azar (misma sala y losa): se agrupan en un solo encuentro."""
from pathlib import Path

from rngauto.core.encounter import load_encounters
from rngauto.core.pokemon import Gender
from rngauto.games.bdsp.encounters import StaticTargetFinder, encounter_species, static_template, target_label
from rngauto.games.bdsp.xorshift import XorshiftState

ENCOUNTERS = load_encounters(Path(__file__).resolve().parents[2] / "encounters" / "bdsp")

GROUPS = {
    "hansa_regice": {"regirock", "regice", "registeel"},
    "hansa_kanto": {"articuno", "zapdos", "moltres"},
    "hansa_johto": {"raikou", "entei", "suicune"},
    "hansa_empatia": {"latias", "latios"},
}


def test_random_species_share_a_single_encounter():
    for enc_id, species in GROUPS.items():
        assert set(ENCOUNTERS[enc_id].species) == species
    grouped = [s for enc in ENCOUNTERS.values() for s in (enc.species or (enc.template,))]
    assert len(grouped) == len(set(grouped)), "una especie no puede estar en dos encuentros"


def test_names_do_not_mention_the_game_version():
    for enc in ENCOUNTERS.values():
        assert "Diamante" not in enc.display_name and "Perla" not in enc.display_name
        if enc.id.startswith("hansa_"):
            assert "Losa" in enc.display_name


def test_group_members_generate_identically():
    """La plantilla del encuentro vale para todas: misma generación, solo cambian stats base y género fijo."""
    state = XorshiftState(0x12345678, 0x9ABCDEF0, 0x0FEDCBA9, 0x87654321)
    for enc_id in GROUPS:
        results = []
        for tpl in encounter_species(ENCOUNTERS[enc_id]):
            finder = StaticTargetFinder(tpl)
            results.append([(t.traits.pid, t.traits.ivs, t.traits.nature, t.traits.ability_slot, t.traits.shiny)
                            for t in finder.neighbourhood(state, 500, 50)])
        assert all(r == results[0] for r in results), enc_id


def test_target_label_hides_unknowable_gender():
    enc = ENCOUNTERS["hansa_empatia"]
    traits = StaticTargetFinder(static_template(enc.template)).generate(XorshiftState(1, 2, 3, 4), 10)
    assert traits.gender is not Gender.GENDERLESS  # la plantilla sola diría ♂ (Latios)
    label = target_label(enc, traits)
    assert label.startswith("Latias/Latios · ")
    assert "♂" not in label and "♀" not in label
    assert target_label(ENCOUNTERS["hansa_regice"], traits).startswith("Regirock/Regice/Registeel · ")
