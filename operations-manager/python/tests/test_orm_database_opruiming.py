"""Achtergebleven testdatabases: de wees moet weg, de levende run moet blijven.

Waarom er uberhaupt opgeruimd moet worden: een run die hard eindigt ruimt zelf niets op.
Dat gold voor de wegwerpcontainer die er eerder per run was -- gemeten op 20 augustus
2026 stonden er elf, de oudste 45 uur, want de container draait onder de Docker-daemon
en niet onder pytest -- en het geldt nu voor de database die elke run in de gedeelde
server maakt.

Zomaar alles opruimen mag niet. Hier draaien suites naast elkaar (agents in eigen
worktrees), en een levende run zijn database onder de voeten weghalen gaf ooit veertig
fouten. Vandaar de pid: die zat eerst als etiket op de container en zit nu in de naam van
de database, en alleen wat een DODE maker heeft is een wees.

Een pid alleen was niet genoeg. Sessies draaien in eigen containers tegen dezelfde
Postgres, en ``os.kill`` kijkt in de EIGEN pid-namespace: de levende run van de buurman
zag er dood uit en werd weggegooid (16 september 2026, 62 ERRORs aan beide kanten). De
naam draagt daarom ook de namespace en het tijdstip.

Deze tests draaien zonder Docker; ze toetsen de beslislogica, niet de dockeraanroep.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import ZAD_TEST_DB_MAX_LEEFTIJD_S, ZAD_TEST_DB_PREFIX, _is_wees, _maker_leeft, _pid_namespace


def test_de_eigen_pid_leeft() -> None:
    assert _maker_leeft(str(os.getpid())) is True


def test_een_dode_pid_is_een_wees() -> None:
    # Een net gestorven kindproces: pid bestond zojuist gegarandeerd, en is nu zeker weg.
    kind = subprocess.Popen([sys.executable, "-c", "pass"])
    kind.wait()
    assert _maker_leeft(str(kind.pid)) is False


def test_zonder_pid_is_het_een_wees() -> None:
    """Iets van voor deze regeling draagt geen pid; die run is hoe dan ook voorbij."""
    assert _maker_leeft("") is False


def test_rommel_in_de_pid_is_een_wees() -> None:
    assert _maker_leeft("geen-getal") is False


def test_de_fixture_zet_de_eigen_pid_in_de_databasenaam() -> None:
    """De opruiming werkt alleen als de maker zijn pid ook echt achterlaat.

    Niet met een draaiende Postgres gemeten (dat kost Docker en seconden), maar op de
    bron: de fixture moet de eigen pid in de naam zetten, want daaraan herkent een volgende
    run in dezelfde namespace een wees.
    """
    import inspect

    import conftest

    bron = inspect.getsource(conftest._orm_db_url.__wrapped__)
    assert "ZAD_TEST_DB_PREFIX" in bron, "de fixture gebruikt de prefix niet meer"
    assert "os.getpid()" in bron, "de databasenaam wordt niet met de eigen pid gevuld"


def _naam(pid: int | str, gemaakt: float, namespace: str | None = None) -> str:
    return f"{ZAD_TEST_DB_PREFIX}{namespace or _pid_namespace()}_{pid}_{int(gemaakt)}"


NU = 1_800_000_000.0
VREEMDE_NAMESPACE = "4026534473"


def _dode_pid() -> int:
    kind = subprocess.Popen([sys.executable, "-c", "pass"])
    kind.wait()
    return kind.pid


class TestWeesOfNiet:
    """Wat mag de veeg weghalen, en wat nooit."""

    def test_een_dode_maker_in_de_eigen_namespace_is_een_wees(self) -> None:
        assert _is_wees(_naam(_dode_pid(), NU), NU) is True

    def test_een_levende_maker_in_de_eigen_namespace_blijft(self) -> None:
        assert _is_wees(_naam(os.getpid(), NU), NU) is False

    def test_een_verse_run_uit_een_andere_namespace_blijft(self) -> None:
        """De bug: zijn pid bestaat hier niet, maar de run draait wel degelijk."""
        vreemd = _naam(_dode_pid(), NU, namespace=VREEMDE_NAMESPACE)

        assert _is_wees(vreemd, NU) is False

    def test_een_oude_run_uit_een_andere_namespace_mag_weg(self) -> None:
        """Anders lekt elke container die hard eindigt een database die niemand opruimt."""
        vreemd = _naam(_dode_pid(), NU - ZAD_TEST_DB_MAX_LEEFTIJD_S - 1, namespace=VREEMDE_NAMESPACE)

        assert _is_wees(vreemd, NU) is True

    def test_de_grens_zelf_telt_nog_niet_als_wees(self) -> None:
        vreemd = _naam(_dode_pid(), NU - ZAD_TEST_DB_MAX_LEEFTIJD_S, namespace=VREEMDE_NAMESPACE)

        assert _is_wees(vreemd, NU) is False

    @pytest.mark.parametrize(
        "naam",
        [
            f"{ZAD_TEST_DB_PREFIX}4867",
            f"{ZAD_TEST_DB_PREFIX}{VREEMDE_NAMESPACE}_4867",
            f"{ZAD_TEST_DB_PREFIX}",
            f"{ZAD_TEST_DB_PREFIX}{VREEMDE_NAMESPACE}_4867_gisteren",
        ],
        ids=["oude-vorm", "zonder-tijd", "leeg", "rommel-in-de-tijd"],
    )
    def test_wat_niet_te_beoordelen_is_blijft_staan(self, naam: str) -> None:
        assert _is_wees(naam, NU) is False


def test_de_namespace_is_die_van_dit_proces() -> None:
    assert _pid_namespace() == str(os.stat("/proc/self/ns/pid").st_ino)


def test_de_fixture_zet_de_namespace_in_de_databasenaam() -> None:
    import inspect

    import conftest

    bron = inspect.getsource(conftest._orm_db_url.__wrapped__)

    assert "_pid_namespace()" in bron, "de databasenaam draagt de namespace niet"
