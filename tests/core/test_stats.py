import json

import pytest

from rngauto.core.pokemon import nature_index
from rngauto.core.stats import StatsInconsistent, compute_stats, iv_ranges_from_stats, nature_modifiers
from rngauto.games.bdsp.encounters import static_template

from ..bdsp.test_static_gen import STATIC_JSON, needs_oracle, template_by_index


def test_nature_modifiers():
    assert nature_modifiers(nature_index("Modest")) == (10, 9, 10, 11, 10, 10)  # +SpA −Atk
    assert nature_modifiers(nature_index("Timid")) == (10, 9, 10, 10, 10, 11)  # +Spe −Atk
    assert nature_modifiers(nature_index("Hardy")) == (10,) * 6


@needs_oracle
def test_stats_match_pokefinder_vectors():
    data = json.loads(STATIC_JSON.read_text("utf-8"))
    checked = 0
    for section in ("generate", "generateRoamer"):
        for case in data[section]:
            tpl = template_by_index(case["category"], case["pokemon"])
            for r in case["results"]:
                assert list(compute_stats(tpl.base_stats, tuple(r["ivs"]), r["level"], r["nature"])) == r["stats"]
                checked += 1
    assert checked >= 70


def test_iv_ranges_roundtrip_dialga():
    tpl = static_template("dialga")
    ivs = (31, 9, 27, 21, 31, 31)
    nature = nature_index("Modesta")
    stats = compute_stats(tpl.base_stats, ivs, tpl.level, nature)
    ranges = iv_ranges_from_stats(tpl.base_stats, tpl.level, nature, stats)
    assert all(r.lo <= v <= r.hi for v, r in zip(ivs, ranges))
    assert all(r.hi - r.lo <= 2 for r in ranges)  # a nivel 47 cada stat acota el IV a 1-3 valores
    with pytest.raises(StatsInconsistent):
        iv_ranges_from_stats(tpl.base_stats, tpl.level, nature, (1, 1, 1, 1, 1, 1))
