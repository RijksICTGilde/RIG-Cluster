"""Backups is een eigen tabblad geworden (RC-100).

Het backupblok stond op het tabblad Deployments, als een van de blokken die de diensten
per deployment leveren (``UIEvent.DEPLOYMENT_SECTIONS``). Het is groot genoeg voor een
eigen aanzicht en het is een BESTEMMING - je gaat erheen om iets terug te zetten - dus
staat het nu waar Metrics ook staat: een tabblad met een deployment per pagina en zijn
naam in het pad.

Wat hier bewaakt wordt:

1. het tabblad bestaat, draagt zijn deployment in het pad en heeft een route;
2. het blok staat NIET meer op het tabblad Deployments - verhuisd, niet gekopieerd;
3. het blok komt alleen op het tabblad Backups terecht, en alleen voor een project dat
   iets kan backuppen;
4. het oude adres met het tabblad voorop (``/projects/backups/<naam>``) bestaat bewust
   niet: dat tabblad heeft nooit onder die vorm geleefd.

Het GEDRAG in de browser (welke deployment de pagina toont, de dialogen, het ene luie
verzoek) staat in tests/e2e/test_lotc_backups_tab.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.web.lotc_switch import PROJECT_TABS, TABS_MET_DEPLOYMENT, project_tab_url, tab_from_path
from opi.web.router import web_router

WORTEL = Path(__file__).resolve().parent.parent
TABS = WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"


def _tabblad(naam: str) -> str:
    """Het stuk sjabloon van een tabblad, zonder zijn toelichting.

    De toelichting noemt de afwegingen en dus ook wat er juist NIET hoort te staan; op de
    ruwe bron gemeten meet je het commentaar mee.
    """
    bron = TABS.read_text()
    start = bron.index(f"{{% elif active_tab == '{naam}' %}}")
    volgende = bron.find("{% elif active_tab ==", start + 10)
    stuk = bron[start : volgende if volgende != -1 else len(bron)]
    return re.sub(r"\{#.*?#\}", "", stuk, flags=re.DOTALL)


# --- 1. Het tabblad zelf ---------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """De webroutes zonder de rest van de applicatie: genoeg om paden te meten."""
    app = FastAPI()
    app.include_router(web_router)
    return TestClient(app)


def test_backups_is_een_tabblad_met_een_deployment_in_het_pad() -> None:
    assert PROJECT_TABS["backups"] == {"label": "Backups", "path": "backups"}
    assert "backups" in TABS_MET_DEPLOYMENT
    assert project_tab_url("demo", "backups", deployment="productie") == "/projects/demo/backups/productie"
    assert tab_from_path("/projects/demo/backups/productie") == "backups"


def test_beide_adresvormen_zijn_letterlijk_geregistreerd() -> None:
    """Met en zonder deployment, en niet als ``/projects/{project}/{tab}``.

    Dat laatste zou ook ``/projects/details/<naam>`` opvangen; zie
    tests/test_lotc_tabbladen_url.py, waar dat voor alle tabbladen tegelijk gemeten wordt.
    """
    paden = {route.path for route in web_router.routes}

    assert "/projects/{project_name}/backups" in paden
    assert "/projects/{project_name}/backups/{deployment_name}" in paden


def test_het_tabblad_neemt_dezelfde_kiezer_op() -> None:
    """Dezelfde kiezer als Deployments en Metrics, uit hetzelfde bestand: opnieuw moeten
    kiezen na het wisselen van tabblad is precies wat RC-92 heeft opgelost."""
    assert 'include "bg/_deployment-selector.html.j2"' in _tabblad("backups")


def test_het_oude_adres_met_het_tabblad_voorop_bestaat_niet(client: TestClient) -> None:
    """De andere tabbladen hebben een doorverwijzing omdat hun oude vorm gedeeld kan zijn.

    Backups bestaat pas sinds RC-100 en heeft nooit onder die vorm geleefd, dus is er niets
    om te laten blijven werken. Een doorverwijzing voor een adres dat niemand kan hebben is
    onderhoud zonder lezer; dit legt die keuze vast.
    """
    assert client.get("/projects/backups/demo", follow_redirects=False).status_code == 404


# --- 2. Verhuisd, niet gekopieerd ------------------------------------------------------


def test_het_deploymenttabblad_toont_het_backupblok_niet_meer() -> None:
    """Twee weergaven van dezelfde gegevens lopen uit de pas; er hoort er een te zijn."""
    deployments = _tabblad("deployments")

    assert "backups_sections" not in deployments
    assert "section-backups" not in deployments


def test_het_backupblok_hangt_niet_meer_aan_de_deploymentsectiehaak() -> None:
    """De haak levert alles op EEN tabblad af, dus een blok dat er nog aan hangt staat
    daar ook - naast het eigen tabblad."""
    from opi.services.catalog.shared import backups

    assert not hasattr(backups.BackupsPageMixin, "backups_block")


# --- 3. Het blok staat op het tabblad Backups ------------------------------------------


def test_het_tabblad_rendert_de_blokken_die_de_route_aanlevert() -> None:
    """Via hetzelfde include als de dienstblokken op Deployments: de context heet
    ``sections``, dus een blok van een dienst rendert hier op dezelfde manier."""
    tabblad = _tabblad("backups")

    assert "sections = backups_sections" in tabblad
    assert 'include "bg/_deployment-service-sections.html.j2"' in tabblad


def test_de_route_vult_het_blok_alleen_op_het_tabblad_backups() -> None:
    """Anders wordt er per paginaweergave een backupsectie gebouwd die niemand ziet."""
    import inspect

    from opi.web import router

    bron = inspect.getsource(router.render_project_page)
    aanroep = bron.index("collect_backups_sections(")
    voorwaarde = bron.rindex('tab_from_path(request.url.path) == "backups"', 0, aanroep)
    assert voorwaarde > 0, "de backupsecties worden niet achter het tabblad gehouden"


def test_een_project_zonder_backupbare_dienst_krijgt_een_uitleg_en_geen_leegte() -> None:
    """Zwijgen leest hier als "de backups zijn weg"."""
    tabblad = _tabblad("backups")

    assert "Geen backups voor deze deployment" in tabblad
    assert "Nog geen deployments" in tabblad, "een project zonder deployments hoort ook iets te lezen"
