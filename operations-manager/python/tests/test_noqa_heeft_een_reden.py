"""Elke onderdrukte beveiligingsregel in ``opi/`` moet een reden bij zich dragen.

De S-regels van ruff staan sinds RC-310 aan voor ``opi/``. Een melding is opgelost of
staat met een ``# noqa`` uit; zonder reden erachter is zo'n onderdrukking niet te
beoordelen en groeit het aantal stil weer. Deze test maakt dat rood.
"""

import re
from pathlib import Path

OPI = Path(__file__).resolve().parent.parent / "opi"

# De noqa en de codes, plus wat er na de laatste code nog op de regel staat.
NOQA = re.compile(r"#\s*noqa:\s*(S\d{3}(?:\s*,\s*S\d{3})*)(?P<rest>.*)$")


def _onderdrukkingen_zonder_reden(root: Path = OPI) -> list[str]:
    zonder = []
    for bestand in sorted(root.rglob("*.py")):
        for nummer, regel in enumerate(bestand.read_text().splitlines(), start=1):
            treffer = NOQA.search(regel)
            if treffer and not treffer.group("rest").strip(" #"):
                zonder.append(f"{bestand.relative_to(root.parent)}:{nummer}")
    return zonder


def test_elke_s_noqa_heeft_een_reden() -> None:
    zonder = _onderdrukkingen_zonder_reden()
    assert not zonder, "noqa zonder reden erachter: " + ", ".join(zonder)


def test_een_noqa_zonder_reden_wordt_gevonden(tmp_path: Path) -> None:
    (tmp_path / "zonder.py").write_text('PASSWORD = "x"  # noqa: S105\n')
    (tmp_path / "met.py").write_text('PASSWORD = "x"  # noqa: S105  # naamconstante\n')

    assert _onderdrukkingen_zonder_reden(tmp_path) == [f"{tmp_path.name}/zonder.py:1"]
