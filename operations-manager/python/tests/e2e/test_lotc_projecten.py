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
    for naam in re.findall(r"/projects/([a-z0-9-]+)/details", html):
        if naam not in volgorde:
            volgorde.append(naam)
    return volgorde


def test_zonder_zoekterm_staan_alle_projecten_er(client: httpx.Client) -> None:
    namen = _projectnamen(client.get("/projects").text)
    assert namen == ["test-project-detail", "test-project", "test-project-services"], namen


def test_de_zoekterm_filtert_op_naam_en_omschrijving(client: httpx.Client) -> None:
    assert _projectnamen(client.get("/projects?q=services").text) == ["test-project-services"]
    # "detailpagina" komt alleen in de OMSCHRIJVING voor, niet in een naam.
    assert _projectnamen(client.get("/projects?q=detailpagina").text) == ["test-project-detail"]


def test_een_zoekterm_zonder_treffers_zegt_dat_ook(client: httpx.Client) -> None:
    antwoord = client.get("/projects?q=bestaatniet").text
    assert _projectnamen(antwoord) == []
    assert "Geen projecten gevonden voor" in antwoord


def test_de_sortering_keert_de_volgorde_echt_om(client: httpx.Client) -> None:
    oplopend = _projectnamen(client.get("/projects?sort=naam").text)
    aflopend = _projectnamen(client.get("/projects?sort=naam-af").text)
    assert aflopend == list(reversed(oplopend)), (oplopend, aflopend)


def test_de_pagina_levert_zelf_het_stuk_dat_het_zoekveld_ververst(client: httpx.Client) -> None:
    """htmx haalt /projects op en pakt daar #projects-lijst uit; die id moet er dus zijn.

    Er was een apart fragmentadres (/projects/lijst). Dat is weg: hx-push-url zette dat
    adres in de adresbalk, en wie dan verversde of de link deelde kreeg een kale lijst
    zonder navigatie of voettekst. Bovendien is een tweede adres met dezelfde gegevens
    een tweede plek die kan gaan afwijken.

    Wat er geswapt wordt is inmiddels het hele ZOEKGEBIED en niet alleen de lijst. Eerst
    lag de werkbalk buiten het geswapte stuk, en dan bleef het vinkje op de oude sortering
    staan en sprong je zoekterm terug. Zoekterm, sortering en resultaat zijn een antwoord,
    dus ze worden samen vervangen. Deze test toetste nog de oude, kleinere swap.
    """
    antwoord = client.get("/projects?q=detail").text

    assert 'id="projects-zoekgebied"' in antwoord, "het doel van hx-select staat niet in de pagina"
    assert 'hx-select="#projects-zoekgebied"' in antwoord, "het formulier pakt niet het juiste stuk uit het antwoord"
    assert 'hx-target="#projects-zoekgebied"' in antwoord, "het formulier zet het antwoord niet op het zoekgebied terug"
    assert 'id="projects-lijst"' in antwoord, "de lijst zelf staat niet in de pagina"
    assert 'hx-get="/projects"' in antwoord, "het formulier haalt niet de pagina zelf op"
    assert _projectnamen(antwoord) == ["test-project-detail"]


def test_de_telling_boven_de_lijst_zegt_hoeveel_er_zijn_en_hoeveel_je_ziet(client: httpx.Client) -> None:
    """Zoeken filtert de LIJST, en de telling zegt allebei de getallen.

    Hier stond een toets op de tegel "Je projecten", die niet mocht dalen als je iets
    intypte. Die vier tegels zijn op verzoek weggehaald, dus wat overblijft is de telling -
    en die hoort juist wel te zeggen wat je zoekterm overlaat, zonder te verzwijgen hoeveel
    er in totaal zijn.
    """
    antwoord = client.get("/projects?q=detail").text

    assert "Totaal: 1 project van 3" in antwoord, "de telling noemt niet allebei de getallen"


def test_de_pagina_toont_de_kaarten_en_wat_daar_omheen_hoort(client: httpx.Client) -> None:
    """Kaarten met naam en omschrijving, plus de dingen die WEL terug moesten.

    Hier stond een toets op de vijf tabelkolommen van het origineel. Die tabel is er op
    verzoek van de gebruiker weer uit: op deze pagina zoek je een project en open je het,
    en omgeving/team/services staan op de projectpagina zelf. Dat is een KEUZE op grond
    van beide beelden, en dus toetst deze test hem nu.

    Wat er wel bij hoort te blijven staan is alles dat bij het omzetten stilletjes wegviel
    zonder dat iemand daarom vroeg: de knop Vernieuwen en de telling. De vier totalen zijn
    later op verzoek weggehaald - stil verdwijnen en bewust weghalen zijn niet hetzelfde.
    """
    antwoord = client.get("/projects").text

    assert "Vernieuwen" in antwoord, "de knop Vernieuwen is weg"
    assert "Totaal: 3 projecten" in antwoord, "de telling boven de lijst is weg"

    # De vier totalen onderaan zijn op verzoek WEGGEHAALD: de aantallen staan in de lijst
    # eronder en je eigen rol is geen kerncijfer. Ze horen dus niet terug te komen.
    for totaal in ("Je projecten", "Teamleden totaal", "Services actief", "Je rol"):
        assert totaal not in antwoord, f"het weggehaalde totaal '{totaal}' staat er weer"

    # Per project een kaart met de naam en de omschrijving, en verder niets.
    assert "Detail Test Project" in antwoord, "de weergavenaam staat niet op de kaart"
    assert "Uitgebreid testproject voor de detailpagina E2E tests" in antwoord, "de omschrijving staat niet op de kaart"

    # En de kolommen die de gebruiker er bewust af wilde hebben, staan er ook echt niet.
    for weg in (">Omgeving<", ">Team<", ">Acties<", "2 leden"):
        assert weg not in antwoord, f"{weg} staat er nog; de tabelkolommen zijn niet weg"


def test_de_korte_projectnaam_staat_naast_de_weergavenaam(client: httpx.Client) -> None:
    """De unieke korte naam hoort in het overzicht, klein en naast de weergavenaam.

    De weergavenaam is niet uniek en is niet wat je in de CLI of de API intypt. Zonder de
    korte naam kun je hier dus wel een project herkennen maar er niet mee verder.

    Hij heeft in de count-plek van de kaart gestaan. Dat is de plek voor een TELLING
    ("123 apps", zo staat het in de registry van het component) en een projectcode is dat
    niet; hij kwam daar terecht omdat het toevallig kleine tekst naast de titel is. Nu
    staat hij in de chipsleuf, waar ook de bewerkdatum staat.

    De vorm wordt meegetoetst en niet alleen de aanwezigheid: niet in de titel en niet in
    de omschrijvingsregel. Zou iemand hem in de titel zetten, dan is het geen
    identificatie meer maar een tweede naam.
    """
    antwoord = client.get("/projects").text

    kaart = antwoord.split('href="/projects/test-project-detail/details"', 1)[1].split("</a>", 1)[0]
    assert 'class="lotc-layer-chips"' in kaart, "de kaart heeft geen chipsleuf"
    assert "test-project-detail" in kaart.split('class="lotc-layer-chips"', 1)[1], (
        "de korte naam staat niet als chip op de kaart"
    )

    # De titel blijft de WEERGAVEnaam.
    assert '<span class="lotc-layer-title">Detail Test Project</span>' in antwoord, (
        "de weergavenaam is niet meer de titel van de kaart"
    )


def test_een_project_zonder_weergavenaam_toont_zijn_naam_niet_twee_keer(client: httpx.Client) -> None:
    """Zonder weergavenaam is de titel al de korte naam; er hoeft er dan niet nog een bij.

    ``lotc_project_rows()`` vult ``display_name`` met de naam zelf als het project er geen
    heeft, dus zonder deze uitzondering zou dezelfde tekst twee keer op de kaart staan.
    """
    antwoord = client.get("/projects?q=test-project&sort=naam").text

    # test-project heeft geen display-name in zijn fixture.
    assert '<span class="lotc-layer-title">test-project</span>' in antwoord, "de fixture is veranderd"
    assert "<code>\n    test-project\n</code>" not in antwoord, (
        "de korte naam staat er dubbel op een project zonder weergavenaam"
    )


def test_zoeken_en_sorteren_staan_in_de_toolbar(client: httpx.Client) -> None:
    """De vorm die de gebruiker aanleverde: zoekveld links, sorteerknop met menu rechts.

    Toetst de OPBOUW en niet het uiterlijk, want daar zit de valkuil: <c-toolbar> gooit
    elk kind weg (het component heeft geen standaard-slot), dus het zoekveld verdween
    zonder foutmelding. Deze test slaat aan zodra dat weer gebeurt.
    """
    antwoord = client.get("/projects").text

    assert "<nldd-toolbar" in antwoord, "de toolbar staat er niet"
    opgeslokt = "het zoekveld staat niet in de toolbar - opgeslokt door een component zonder slot?"
    assert 'slot="start"' in antwoord, opgeslokt
    assert "nldd-search-field" in antwoord, opgeslokt
    assert 'slot="end"' in antwoord, "de sorteerknop staat niet in de toolbar"
    # GEEN slot="overflow" meer, en dat is het omgekeerde van wat hier stond. De eigen
    # overloopgroep is weg omdat de enkele aanwezigheid ervan de toolbar permanent een
    # "Meer"-knop laat tonen: het sorteren stond daardoor dubbel op het scherm, ook op 1440
    # breed, en de items in die groep werden nooit klikbaar. Het geeft niets op, want de
    # overloop van dit component werkt sowieso niet - zonder de groep verschijnt op 1024px
    # wel een "Meer"-knop maar is zijn menu leeg. De volledige meting staat in
    # request_for_components.md; tests/test_lotc_toolbar_overloop.py houdt hem eruit.
    assert 'slot="overflow"' not in antwoord, "de eigen overloopgroep is terug"

    # Een UITKLAPMENU en geen menuBALK. <c-menu> rendert altijd nldd-menu-bar, en die
    # bleef in de popup-slot leeg; daarom staat het menu hier als nldd-markup. Slaat dit
    # aan, dan is iemand teruggegaan naar het component en is het menu weer leeg.
    assert '<nldd-menu slot="popup"' in antwoord, "het sorteermenu is geen nldd-menu in de popup-slot"
    assert "nldd-menu-bar" not in antwoord.split("<nldd-toolbar")[1].split("</nldd-toolbar>")[0], (
        "er staat een menuBALK in de toolbar; die blijft leeg in een popup"
    )

    # De sorteeropties zijn LINKS: dan werkt sorteren ook zonder JavaScript.
    #
    # Het zijn er TWEE. Hier stonden er vier; "Meeste deployments" en "Meeste teamleden"
    # zijn er in 80da844c uit gehaald omdat niemand ze gebruikte. Die test bleef staan en
    # sloeg daarna aan op een verwijdering die met opzet gedaan was. De lijst hoort mee te
    # bewegen met PROJECT_SORTERINGEN in opi/web/lotc_switch.py.
    for sleutel in ("naam", "naam-af"):
        assert f"/projects?sort={sleutel}" in antwoord, f"sorteeroptie {sleutel} is geen link"

    for weg in ("deployments", "teamleden"):
        assert f"/projects?sort={weg}" not in antwoord, (
            f"sorteeroptie {weg} is er weer; die is bewust verwijderd (80da844c)"
        )

    # En de gekozen sortering is aangevinkt: het item met de huidige sortering draagt
    # selected, de andere niet.
    huidige = re.search(r'<nldd-menu-item[^>]*href="/projects\?sort=naam"[^>]*>', antwoord)
    assert huidige, "de standaardsortering staat niet als menu-item in de pagina"
    assert "selected" in huidige.group(0), f"de gekozen sortering is niet gemarkeerd: {huidige.group(0)}"
