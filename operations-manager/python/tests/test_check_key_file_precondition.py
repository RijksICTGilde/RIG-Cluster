"""De `_check-key-file` precondition in de Taskfile: een sleutel- of tokenbestand dat voor
groep of anderen leesbaar of schrijfbaar is, moet de taak die het gebruikt laten stoppen
voordat er iets gebeurt.

Deze toets draait het shell-fragment van de precondition rechtstreeks met bash, niet via
go-task: `_check-key-file` is `internal` en heeft geen `cmds`, dus er is niets om als taak
te draaien. Het fragment zelf is de kern van RC-311, inclusief de octale val die de commit
noemt (een leidende nul die de shell van task niet als octaal leest, waardoor een goed
bestand werd afgewezen); die val hoort hier rood te worden als hij terugkomt.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"

#: Taken die een sleutel- of tokenpad rechtstreeks in hun eigen `cmds` noemen (niet alleen
#: doorgeven aan een subtaak) maar de guard niet hebben, met de reden. Wat hier bij komt,
#: is een lezer van geheim materiaal zonder rechtencontrole: een reviewer kijkt naar deze
#: lijst, niet naar de hele Taskfile.
GEEN_GUARD_NODIG: dict[str, str] = {
    "generate-age-key": "leest alleen de publieke tweede regel (sed -n '2p'), of maakt de sleutel aan",
    "sandbox:generate-age-key": "leest alleen de publieke tweede regel (sed -n '2p'), of maakt de sleutel aan",
    "sandbox:destroy": "verwijdert het sleutelbestand (rm -f), leest de inhoud niet",
    "argocd:trust-public-key": (
        "KEY_FILE is daar een PUBLIEKE GPG-sleutel en geen geheim; de guard zou een"
        " publieke sleutel met de gebruikelijke 644 juist afwijzen"
    ),
    "upgrade-safety-identity": (
        "leest de sleutel zelf via compare_service_identity.py --key-file, buiten de lijst van het plan;"
        " draait met dir: operations-manager/python, waardoor een dep-taak in de root het verkeerde pad krijgt"
    ),
}

_SLEUTELPAD = re.compile(
    r"\{\{\.KEY_FILE\}\}|\{\{\.KEY\}\}|security/sandbox-key\.txt|security/developer-key\.txt|security/key\.txt"
)


def _taken() -> dict[str, Any]:
    return yaml.safe_load(TASKFILE.read_text())["tasks"]


def _precondition_sh() -> str:
    return _taken()["_check-key-file"]["preconditions"][0]["sh"]


def _heeft_de_guard(taak: dict[str, Any]) -> bool:
    for dep in taak.get("deps") or []:
        if dep == "_check-key-file" or (isinstance(dep, dict) and dep.get("task") == "_check-key-file"):
            return True
    return False


def _leest_rechtstreeks_een_sleutelpad(taak: dict[str, Any]) -> bool:
    """Een letterlijke shellregel in `cmds` die het pad noemt, geen doorgave aan een subtaak.

    Een `task: andere-taak` met `vars: {KEY_FILE: ...}` geeft het pad door aan een taak die
    zijn eigen guard draagt (zie `_generate-secrets-shared` en de taken die hem aanroepen),
    en telt hier niet mee.
    """
    return any(isinstance(cmd, str) and _SLEUTELPAD.search(cmd) for cmd in taak.get("cmds") or [])


def _draai(key_file: Path) -> subprocess.CompletedProcess[str]:
    script = _precondition_sh().replace("{{.KEY_FILE}}", str(key_file))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_een_sleutel_alleen_voor_de_eigenaar_leesbaar_gaat_door(tmp_path: Path, mode: int) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("whatever\n")
    key_file.chmod(mode)

    resultaat = _draai(key_file)

    assert resultaat.returncode == 0, resultaat.stdout + resultaat.stderr


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o606])
def test_een_sleutel_die_groep_of_anderen_kunnen_lezen_stopt(tmp_path: Path, mode: int) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("whatever\n")
    key_file.chmod(mode)

    resultaat = _draai(key_file)

    assert resultaat.returncode == 1, resultaat.stdout + resultaat.stderr


def test_een_ontbrekend_bestand_stopt_hier_niet_maar_bij_de_aanroeper(tmp_path: Path) -> None:
    resultaat = _draai(tmp_path / "nope.txt")

    assert resultaat.returncode == 0, resultaat.stdout + resultaat.stderr


def test_een_symlink_naar_een_sleutel_met_goede_rechten_gaat_door(tmp_path: Path) -> None:
    """stat zonder -L leest symlink-modus (altijd 777), niet het doelbestand."""
    echte_sleutel = tmp_path / "echte-sleutel.txt"
    echte_sleutel.write_text("whatever\n")
    echte_sleutel.chmod(0o600)
    link = tmp_path / "key.txt"
    link.symlink_to(echte_sleutel)

    resultaat = _draai(link)

    assert resultaat.returncode == 0, resultaat.stdout + resultaat.stderr


def test_een_symlink_naar_een_sleutel_met_slechte_rechten_stopt(tmp_path: Path) -> None:
    echte_sleutel = tmp_path / "echte-sleutel.txt"
    echte_sleutel.write_text("whatever\n")
    echte_sleutel.chmod(0o644)
    link = tmp_path / "key.txt"
    link.symlink_to(echte_sleutel)

    resultaat = _draai(link)

    assert resultaat.returncode == 1, resultaat.stdout + resultaat.stderr


def test_elke_taak_die_een_sleutelpad_rechtstreeks_leest_heeft_de_guard() -> None:
    """Het plan vraagt deze guard op elke lezer; dit toetst dat die belofte blijft gelden.

    Een nieuwe taak die `{{.KEY_FILE}}` of een vast sleutelpad in zijn eigen `cmds` zet,
    hoort `_check-key-file` als dep te krijgen, of een reden in `GEEN_GUARD_NODIG`.
    """
    taken = _taken()
    zonder_guard = [
        naam
        for naam, taak in taken.items()
        if naam != "_check-key-file" and _leest_rechtstreeks_een_sleutelpad(taak) and not _heeft_de_guard(taak)
    ]

    onverwacht = sorted(set(zonder_guard) - set(GEEN_GUARD_NODIG))
    assert not onverwacht, f"lezen een sleutelpad zonder guard en zonder reden: {onverwacht}"


def test_geen_guard_nodig_noemt_geen_taak_die_inmiddels_wel_bestaat_en_gedekt_is() -> None:
    """Een vlag die niemand meer afvangt, is dode documentatie; dat merk je hier."""
    taken = _taken()
    for naam in GEEN_GUARD_NODIG:
        assert naam in taken, f"{naam} staat niet meer in de Taskfile"
        assert not _heeft_de_guard(taken[naam]), (
            f"{naam} heeft de guard inmiddels wel; haal hem uit de uitzonderingslijst"
        )
