"""Las tablas de encuentros de los README cuadran con el ``status`` de cada TOML.

El estado de cada encuentro se guarda en su TOML (``status = "comprobado"``); los README lo repiten para quien
mira el repositorio. Si alguien marca un encuentro como comprobado y olvida los README, este test lo dice.
"""
from pathlib import Path

import pytest

from rngauto.core.encounter import load_encounters
from rngauto.games.bdsp.encounters import encounter_species
from rngauto.gui.state import ENCOUNTERS_DIR

ROOT = Path(__file__).resolve().parents[1]


def _rows(readme: Path) -> list[tuple[str, str]]:
    """(primera columna, última columna) de las filas de la tabla de encuentros."""
    rows = []
    for line in readme.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 2 and not set(cells[0]) <= {"-"}:
            rows.append((cells[0], cells[1]))
    return rows


@pytest.mark.parametrize("readme, tested", [("README.md", "Sí"), ("README.en.md", "Yes")])
def test_readme_tables_match_the_encounter_status(readme, tested):
    rows = _rows(ROOT / readme)
    for enc in load_encounters(ENCOUNTERS_DIR).values():
        for species in encounter_species(enc):
            found = [last for first, last in rows if species.name in first]
            assert found, f"{readme}: falta {species.name} ({enc.id})"
            assert (found[0] == tested) == (enc.status == "comprobado"), (
                f"{readme}: {species.name} figura como «{found[0]}» pero su TOML dice «{enc.status}»")
