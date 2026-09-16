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

import inspect
import os
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import conftest
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
        ids=["alleen-pid", "zonder-tijd", "leeg", "rommel-in-de-tijd"],
    )
    def test_wat_niet_te_beoordelen_is_blijft_staan(self, naam: str) -> None:
        assert _is_wees(naam, NU) is False


def test_de_namespace_is_die_van_dit_proces() -> None:
    assert _pid_namespace() == str(os.stat("/proc/self/ns/pid").st_ino)


def test_de_fixture_zet_de_namespace_in_de_databasenaam() -> None:
    bron = inspect.getsource(conftest._orm_db_url.__wrapped__)

    assert "_pid_namespace()" in bron, "de databasenaam draagt de namespace niet"


def _like(patroon: str, naam: str) -> bool:
    """``naam LIKE patroon`` zoals Postgres hem leest: ``_`` is een teken, ``%`` een reeks."""
    regex = "".join({"_": ".", "%": ".*"}.get(teken, re.escape(teken)) for teken in patroon)
    return re.fullmatch(regex, naam, flags=re.DOTALL) is not None


def test_de_veeg_van_oudere_takken_ziet_de_nieuwe_naam_niet() -> None:
    """Die veeg leest alles onder ``zad_test_%`` als ``zad_test_<pid>`` en crasht op deze vorm."""
    naam = _naam(os.getpid(), NU)

    assert _like("zad_test_%", "zad_test_4867"), "de LIKE-nabootsing klopt niet"
    assert not _like("zad_test_%", naam), f"{naam!r} valt onder de veeg van oudere takken"
    assert _like(f"{ZAD_TEST_DB_PREFIX}%", naam), f"{naam!r} valt niet onder de eigen veeg"


class _NagebootsteServer:
    """Een ``_psql`` die de databases onthoudt in plaats van ze te hebben.

    De veeg praat alleen via ``_psql`` met Postgres, dus hiermee is te meten WELKE
    databases hij weggooit zonder dat er een server hoeft te draaien.
    """

    def __init__(self, namen: list[str]) -> None:
        self.namen = list(namen)
        self.gedropt: list[str] = []

    def __call__(self, sql: str) -> str:
        if sql.startswith("SELECT datname"):
            return "\n".join(self.namen)
        if sql.startswith("DROP DATABASE"):
            self.gedropt.append(sql.split('"')[1])
        return ""


class TestDeVeeg:
    """Niet de beslissing, maar wat er werkelijk wordt weggegooid."""

    def test_de_veeg_haalt_de_wezen_weg_en_laat_de_rest_staan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nu = time.time()
        levende_buurman = _naam(_dode_pid(), nu, namespace=VREEMDE_NAMESPACE)
        eigen_wees = _naam(_dode_pid(), nu)
        oude_vreemde = _naam(_dode_pid(), nu - ZAD_TEST_DB_MAX_LEEFTIJD_S - 60, namespace=VREEMDE_NAMESPACE)
        server = _NagebootsteServer(
            [
                _naam(os.getpid(), nu),
                levende_buurman,
                eigen_wees,
                oude_vreemde,
                f"{ZAD_TEST_DB_PREFIX}4867",
            ]
        )
        monkeypatch.setattr(conftest, "_psql", server)

        conftest._ruim_verweesde_databases_op()

        assert sorted(server.gedropt) == sorted([eigen_wees, oude_vreemde])

    def test_de_veeg_raakt_de_database_van_de_levende_buurman_niet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De storing van 16 september: zijn pid bestaat hier niet, zijn suite draait wel."""
        buurman = _naam(_dode_pid(), time.time(), namespace=VREEMDE_NAMESPACE)
        server = _NagebootsteServer([buurman])
        monkeypatch.setattr(conftest, "_psql", server)

        conftest._ruim_verweesde_databases_op()

        assert server.gedropt == []

    @pytest.mark.parametrize("uitvoer", ["", "\n", "   \n\n  "], ids=["leeg", "enkele-regel", "witruimte"])
    def test_zonder_databases_gooit_de_veeg_niets_weg(self, monkeypatch: pytest.MonkeyPatch, uitvoer: str) -> None:
        server = _NagebootsteServer([])
        monkeypatch.setattr(conftest, "_psql", lambda sql: uitvoer if sql.startswith("SELECT") else server(sql))

        conftest._ruim_verweesde_databases_op()

        assert server.gedropt == []


#: Draait de fixture met een nagebootste psql en schrijft de databasenaam naar stdout.
_KIND = """
import sys
sys.path.insert(0, {testmap!r})
import conftest

conftest._zorg_voor_container = lambda: "55432"
gemaakt = []


def nep_psql(sql):
    if sql.startswith("CREATE DATABASE"):
        gemaakt.append(sql.split('"')[1])
    return ""


conftest._psql = nep_psql
next(conftest._orm_db_url.__wrapped__())
print(gemaakt[0])
"""


def test_een_naam_van_een_gestorven_run_wordt_als_wees_herkend() -> None:
    """De naamvorm van de fixture moet de veeg ook echt iets zeggen.

    Een kindproces maakt de naam met de fixture zelf en sterft. Die naam is VERS, dus
    alleen de pid kan hem als wees aanwijzen, en dat lukt alleen als de veeg de
    namespace erin herkent als de zijne. Draait de volgorde van de velden om, dan leest
    de veeg de naam als die van een vreemde namespace en blijft een dode run staan --
    en andersom verdwijnt de levende buurman weer.
    """
    testmap = os.path.dirname(__file__)
    kind = subprocess.run(
        [sys.executable, "-c", _KIND.format(testmap=testmap)],
        capture_output=True,
        text=True,
        check=True,
    )
    naam = kind.stdout.strip()
    assert naam.startswith(ZAD_TEST_DB_PREFIX), f"de fixture maakte geen testdatabase: {naam!r}"

    assert _is_wees(naam, time.time()) is True, f"de veeg herkent {naam!r} niet als wees van een dode run"
