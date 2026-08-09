"""Zoeken en sorteren op het projectenoverzicht, en wat daar niet weer weg mag.

Waarom deze tests over de HTTP-laag gaan en niet over de browser: zoeken en sorteren
gebeuren op de server. Dat is een keuze - zo werkt het ook zonder JavaScript, is een
gefilterde lijst deelbaar als URL, en kan de telling boven de tabel niet uit de pas
lopen met wat eronder staat. Precies dat gedrag hoort dus op deze hoogte getoetst te
worden; een browsertest zou er alleen een langzamere versie van zijn.

De pagina is hier bovendien een keer HERONTWORPEN in plaats van omgezet ("bewust
sober"), en dat kostte drie kolommen, de knop Vernieuwen, de telling en vier totalen.
Die staan hieronder allemaal apart genoemd, zodat het niet nog eens ongemerkt kan.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import TEST_USER, _sign_session

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.e2e


@pytest.fixture
def client(app_server: str) -> Iterator[httpx.Client]:
    cookie = _sign_session({"user": TEST_USER})
    with httpx.Client(base_url=app_server, cookies={"session": cookie}, follow_redirects=True, timeout=60) as c:
        yield c


def _projectnamen(html: str) -> list[str]:
    """De projecten in de volgorde waarin ze op de pagina staan, zonder dubbelen."""
    volgorde: list[str] = []
    for naam in re.findall(r"/projects/details/([a-z0-9-]+)", html):
        if naam not in volgorde:
            volgorde.append(naam)
    return volgorde


def test_zonder_zoekterm_staan_alle_projecten_er(client: httpx.Client) -> None:
    namen = _projectnamen(client.get("/projects?layout=nldd").text)
    assert namen == ["test-project-detail", "test-project", "test-project-services"], namen


def test_de_zoekterm_filtert_op_naam_en_omschrijving(client: httpx.Client) -> None:
    assert _projectnamen(client.get("/projects?layout=nldd&q=services").text) == ["test-project-services"]
    # "detailpagina" komt alleen in de OMSCHRIJVING voor, niet in een naam.
    assert _projectnamen(client.get("/projects?layout=nldd&q=detailpagina").text) == ["test-project-detail"]


def test_een_zoekterm_zonder_treffers_zegt_dat_ook(client: httpx.Client) -> None:
    antwoord = client.get("/projects?layout=nldd&q=bestaatniet").text
    assert _projectnamen(antwoord) == []
    assert "Geen projecten gevonden voor" in antwoord


def test_de_sortering_keert_de_volgorde_echt_om(client: httpx.Client) -> None:
    oplopend = _projectnamen(client.get("/projects?layout=nldd&sort=naam").text)
    aflopend = _projectnamen(client.get("/projects?layout=nldd&sort=naam-af").text)
    assert aflopend == list(reversed(oplopend)), (oplopend, aflopend)


def test_de_htmx_route_geeft_een_fragment_en_geen_pagina(client: httpx.Client) -> None:
    """/projects/lijst is het doel van het zoekveld; het mag geen hele pagina zijn.

    Kwam er wel een hele pagina terug, dan zou htmx die IN de pagina zetten en stond er
    een tweede navigatie en een tweede voettekst in de tabel.
    """
    antwoord = client.get("/projects/lijst?layout=nldd&q=detail")
    assert antwoord.status_code == 200
    assert "<html" not in antwoord.text.lower()
    assert "<nav" not in antwoord.text.lower()
    assert _projectnamen(antwoord.text) == ["test-project-detail"]


def test_de_totalen_onderaan_volgen_de_zoekterm_niet(client: httpx.Client) -> None:
    """ "Je projecten: 3" hoort niet te dalen omdat je iets in het zoekveld typt.

    De telling BOVEN de tabel hoort dat wel te doen - die gaat over wat je ziet.
    """
    antwoord = client.get("/projects?layout=nldd&q=services").text
    assert "Totaal: 1 project van 3" in antwoord, "de telling boven de tabel volgt de zoekterm niet"

    # De tegel rendert als <span class="lotc-metric-value">3</span> gevolgd door zijn
    # label; op die volgorde wordt gemeten, want het getal alleen komt overal voor.
    tegel = re.search(r'lotc-metric-value">(\d+)</span></div>\s*<div class="lotc-metric-label">Je projecten', antwoord)
    assert tegel, "de tegel 'Je projecten' staat er niet meer"
    assert tegel.group(1) == "3", f"het totaal onderaan is meegefilterd met de zoekterm: {tegel.group(1)}"


def test_de_pagina_toont_nog_alles_wat_de_oude_toonde(client: httpx.Client) -> None:
    """De kolommen, de knop, de telling en de vier totalen van projects-overview.

    Elk van deze is hier een keer verdwenen doordat de pagina opnieuw ontworpen werd in
    plaats van omgezet. Ze staan per stuk genoemd zodat de melding zegt WAT er weg is.
    """
    antwoord = client.get("/projects?layout=nldd").text

    for kolom in ("Project", "Omgeving", "Team", "Services", "Acties"):
        assert f">{kolom}<" in antwoord, f"kolom {kolom} staat niet meer in de tabel"

    assert "Vernieuwen" in antwoord, "de knop Vernieuwen is weg"
    assert "Totaal: 3 projecten" in antwoord, "de telling boven de tabel is weg"
    assert "Details" in antwoord, "de link Details per rij is weg"
    assert "test-project-detail" in antwoord, "de technische projectnaam onder de titel is weg"
    assert "2 leden" in antwoord, "het aantal teamleden per project is weg"
    assert "Local" in antwoord, "de omgeving per project is weg"

    for totaal in ("Je projecten", "Teamleden totaal", "Services actief", "Je rol"):
        assert totaal in antwoord, f"het totaal '{totaal}' onderaan is weg"
