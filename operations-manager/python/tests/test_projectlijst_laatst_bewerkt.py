"""De bewerkdatum in het projectenoverzicht: de opmaak, en waar hij vandaan komt.

Twee dingen worden hier vastgelegd. Ten eerste dat een ontbrekend of onleesbaar
tijdstempel GEEN uitzondering en geen streepje oplevert maar niets: een overzicht dat
niet weet wanneer een project bewerkt is, moet het project nog steeds tonen. Ten tweede
dat de git-doorloop de NIEUWSTE wijziging per bestand oplevert, want dat is de enige
eigenschap waar de hele weergave op leunt.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from opi.web.lotc_switch import filter_lotc_projects, laatst_bewerkt, lotc_project_rows, relatieve_tijd

NU = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _Verzoek:
    """Alleen de query-parameters, want dat is alles wat filter_lotc_projects leest."""

    def __init__(self, **params: str) -> None:
        self.query_params = params


def _drie_projecten() -> list[dict[str, str]]:
    return [
        {"name": "midden", "display_name": "Midden", "description": ""},
        {"name": "oudste", "display_name": "Oudste", "description": ""},
        {"name": "nieuwste", "display_name": "Nieuwste", "description": ""},
    ]


_TIJDEN = {
    "midden": "2026-08-10T12:00:00+00:00",
    "oudste": "2025-01-01T12:00:00+00:00",
    "nieuwste": "2026-08-16T11:00:00+00:00",
}


@pytest.mark.parametrize(
    ("verstreken", "verwacht"),
    [
        (timedelta(seconds=0), "zojuist"),
        (timedelta(seconds=59), "zojuist"),
        (timedelta(seconds=-30), "zojuist"),  # klokverschil, geen negatief getal
        (timedelta(minutes=1), "1 minuut geleden"),
        (timedelta(minutes=5), "5 minuten geleden"),
        (timedelta(minutes=119), "1 uur geleden"),  # naar beneden, niet naar boven
        (timedelta(hours=4), "4 uur geleden"),
        (timedelta(hours=25), "gisteren"),
        (timedelta(days=3), "3 dagen geleden"),
    ],
)
def test_relatieve_tijd_leest_als_nederlands(verstreken: timedelta, verwacht: str) -> None:
    assert relatieve_tijd(verstreken) == verwacht


def test_label_draagt_datum_en_relatieve_tijd() -> None:
    """Beide, want ze beantwoorden verschillende vragen: precies versus begrijpelijk."""
    resultaat = laatst_bewerkt("2026-08-16T08:00:00+00:00", nu=NU)

    assert resultaat is not None
    assert "16 aug 2026" in resultaat["label"]
    assert "geleden" in resultaat["label"]


def test_relatieve_deel_valt_weg_voorbij_een_maand() -> None:
    """'412 dagen geleden' zegt niets dat de datum niet beter zegt."""
    resultaat = laatst_bewerkt("2025-07-01T08:00:00+00:00", nu=NU)

    assert resultaat is not None
    assert resultaat["label"] == "1 jul 2025"


@pytest.mark.parametrize("tijdstempel", [None, "", "gisterenmiddag", "2026-13-45T99:99:99"])
def test_onbruikbaar_tijdstempel_geeft_niets_en_geen_fout(tijdstempel: str | None) -> None:
    assert laatst_bewerkt(tijdstempel, nu=NU) is None


def test_rij_zonder_tijdstempel_toont_geen_bewerkdatum() -> None:
    """Zonder deze val toont de lijst niets meer zodra git niet te lezen is."""
    rijen = lotc_project_rows([{"name": "vr3ed-r0l", "display_name": "Vergunningen"}], None)

    assert rijen[0]["last_modified"] is None
    assert rijen[0]["name"] == "vr3ed-r0l"


def test_rij_koppelt_het_tijdstempel_op_projectnaam() -> None:
    rijen = lotc_project_rows(
        [{"name": "vr3ed-r0l", "display_name": "Vergunningen"}, {"name": "ander", "display_name": "Ander"}],
        {"vr3ed-r0l": datetime.now(UTC).isoformat()},
    )

    assert rijen[0]["last_modified"] is not None
    assert rijen[1]["last_modified"] is None


@pytest.mark.parametrize(
    ("sortering", "verwacht"),
    [
        ("bewerkt", ["nieuwste", "midden", "oudste"]),
        ("bewerkt-op", ["oudste", "midden", "nieuwste"]),
    ],
)
def test_sorteren_op_bewerkdatum_werkt_twee_kanten_op(sortering: str, verwacht: list[str]) -> None:
    rijen = lotc_project_rows(_drie_projecten(), _TIJDEN)

    resultaat = filter_lotc_projects(_Verzoek(sort=sortering), rijen)  # type: ignore[arg-type]

    assert [project["name"] for project in resultaat["projects"]] == verwacht


def test_naam_aflopend_blijft_werken_na_de_richting_in_de_tabel() -> None:
    """De losse ``if gekozen == "naam-af"`` is vervangen; dit is die regressietest."""
    rijen = lotc_project_rows(_drie_projecten(), _TIJDEN)

    oplopend = filter_lotc_projects(_Verzoek(sort="naam"), rijen)  # type: ignore[arg-type]
    aflopend = filter_lotc_projects(_Verzoek(sort="naam-af"), rijen)  # type: ignore[arg-type]

    assert [p["name"] for p in oplopend["projects"]] == ["midden", "nieuwste", "oudste"]
    assert [p["name"] for p in aflopend["projects"]] == ["oudste", "nieuwste", "midden"]


def test_onbekende_sortering_valt_terug_op_de_eerste() -> None:
    rijen = lotc_project_rows(_drie_projecten(), _TIJDEN)

    resultaat = filter_lotc_projects(_Verzoek(sort="onzin"), rijen)  # type: ignore[arg-type]

    assert [p["name"] for p in resultaat["projects"]] == ["midden", "nieuwste", "oudste"]


@pytest.mark.asyncio
async def test_git_doorloop_houdt_de_nieuwste_wijziging_per_bestand() -> None:
    """De hele weergave leunt hierop: git log is nieuwste-eerst, dus de EERSTE telt.

    Zonder de setdefault wint de OUDSTE commit en toont het overzicht bij elk project
    de dag waarop het is aangemaakt.
    """
    from opi.connectors.git import GitConnector

    connector = GitConnector.__new__(GitConnector)
    connector.repo_path = ""
    connector.ensure_repo_cloned = AsyncMock(return_value=True)
    connector._run_git_command = AsyncMock(
        return_value=(
            "\x1e2026-08-16T10:00:00+02:00\nprojects/a.yaml\n"
            "\x1e2026-08-15T09:00:00+02:00\nprojects/a.yaml\nprojects/b.yaml\n"
            "\x1e2026-01-01T09:00:00+02:00\nprojects/a.yaml\n",
            "",
            0,
        )
    )

    resultaat = await connector.last_modified_per_file("projects")

    assert resultaat["projects/a.yaml"] == "2026-08-16T10:00:00+02:00"
    assert resultaat["projects/b.yaml"] == "2026-08-15T09:00:00+02:00"


@pytest.mark.asyncio
async def test_git_doorloop_valt_stil_terug_als_het_commando_faalt() -> None:
    from opi.connectors.git import GitConnector

    connector = GitConnector.__new__(GitConnector)
    connector.repo_path = ""
    connector.ensure_repo_cloned = AsyncMock(return_value=True)
    connector._run_git_command = AsyncMock(return_value=("", "fatal: not a git repository", 128))

    assert await connector.last_modified_per_file("projects") == {}
