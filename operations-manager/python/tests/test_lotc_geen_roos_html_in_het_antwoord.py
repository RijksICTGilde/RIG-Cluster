"""Geen roos-HTML in een LOTC-antwoord (RC-64, fase 3).

DE POORT DIE ONTBRAK

RC-62 heeft de rvo-klassen uit de LOTC-templates gehaald en dat was juist. Wat die
opruiming niet kon zien: de klassen kwamen daarna alsnog binnen, via een TWEEDE
renderomgeving. ``render_roos()`` rendert een sjabloon met de roos-omgeving en zet het
resultaat in de LOTC-pagina; in de bron van die pagina is dan geen enkele ``rvo-`` te
vinden, en in wat de gebruiker krijgt staan er tachtig.

Vandaar dat deze test niet naar sjablonen kijkt maar naar ANTWOORDEN. Twee sporen:

- ``data-roos-component``: het attribuut dat jinja-roos in elke gerenderde component zet.
  LOTC zet ``data-lotc-component``. Een enkel voorkomen bewijst dus dat er HTML uit de
  andere bouwlijn is binnengekomen.
- ``rvo-``: de klassen die daarbij horen. Ze doen op een LOTC-pagina niets - de omgeving
  laadt ``["lotc-layout", "nldd", "lotc-forms"]`` en ``lotc_rvo`` staat daar niet bij - dus
  het blok komt er niet "zichtbaar anders" uit maar volledig onopgemaakt.

WAAROM DE bg-ROUTES EN NIET DE ECHTE ROUTES

De echte routes willen een project uit de store, een taakservice, Keycloak en Prometheus.
``/lotc/bg/<naam>`` rendert dezelfde sjablonen met de voorbeeldprojecten uit
``opi/web/lotc_fixtures/``, die door dezelfde helpers en dezelfde registry lopen als een
echt projectbestand. Het is dus dezelfde pagina met andere gegevens, en dat is precies wat
hier gemeten moet worden.

WAT ER BEWUST BUITEN VALT

``/lotc/pagina/<naam>`` toont de eerste generatie automatisch omgezette sjablonen. Die
hangen aan geen enkele gebruikersroute en dragen hun rvo-resten nog; ze staan geteld in
``tests/test_lotc_geen_rvo_resten.py`` en horen bij een eigen opruimtaak. Meetellen zou
deze poort meteen rood zetten om iets wat geen gebruiker ziet.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.web.lotc_router import REDESIGNED_PAGES
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware

#: Het attribuut dat jinja-roos in elke gerenderde component achterlaat.
ROOS_MARKER = "data-roos-component"


@pytest.fixture(scope="module")
def client() -> TestClient:
    """De bg-routes, met de twee dingen die ze van de applicatie nodig hebben.

    De sessie omdat ``_context`` de ingelogde gebruiker opzoekt, en de uitnodigingsroutes
    omdat het uitnodigingsblok zijn link met ``url_for('invite_landing')`` opbouwt - een
    absolute link uit het verzoek, en niet uit een ingestelde basis-URL.
    """
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    return TestClient(app)


@pytest.mark.parametrize("slug", sorted(REDESIGNED_PAGES))
def test_een_lotc_pagina_bevat_geen_roos_html(slug: str, client: TestClient) -> None:
    antwoord = client.get(f"/lotc/bg/{slug}")

    assert antwoord.status_code == 200, f"/lotc/bg/{slug} rendert niet: {antwoord.status_code}"
    assert ROOS_MARKER not in antwoord.text, (
        f"/lotc/bg/{slug} bevat HTML uit de roos-omgeving. Waarschijnlijk rendert een blok "
        f"nog via render_roos(); leg er een LOTC-tegenhanger naast (zie tests/test_lotc_dienstblokken.py)."
    )
    assert "rvo-" not in antwoord.text, (
        f"/lotc/bg/{slug} bevat rvo-klassen. Op een LOTC-pagina maakt geen enkel stijlblad "
        f"die op: lotc_rvo staat niet in DESIGN_SYSTEMS."
    )


@pytest.mark.parametrize("tab", ["project", "deployments", "metrics", "backups", "taken"])
def test_elk_tabblad_van_de_projectpagina_blijft_schoon(tab: str, client: TestClient) -> None:
    """De tabbladen zijn echte links met een eigen URL, dus ook eigen antwoorden."""
    antwoord = client.get(f"/lotc/bg/project-tabs?tab={tab}")

    assert antwoord.status_code == 200
    assert ROOS_MARKER not in antwoord.text
    assert "rvo-" not in antwoord.text


def test_de_projectpagina_toont_de_blokken_die_de_diensten_leveren(client: TestClient) -> None:
    """Anders meet de poort hierboven een pagina waar de dienstblokken niet op staan.

    Dit is de helft die stil kan wegvallen: het voorbeeldproject gaf ``[]`` mee voor de
    dienstsecties, en dan is "geen roos-HTML" waar om de verkeerde reden.
    """
    # De dienstblokken staan sinds RC-101 op het tabblad "services-info" en niet meer op
    # Overzicht: ze gaan over hoe je bij een dienst KOMT (een adres, een wachtwoord, een
    # code), en dat is iets anders dan de stand van het project. Deze test volgt ze
    # daarheen; wat hij bewaakt is onveranderd, namelijk dat de proefopstelling ze
    # werkelijk rendert en de poort hierboven dus niet om de verkeerde reden groen staat.
    tekst = client.get("/lotc/bg/project-tabs?tab=services-info").text

    for blok in ("Bijlagen", "Uitnodigingen", "Keycloak"):
        assert blok in tekst, f"het dienstblok {blok} staat niet op de proefopstelling"
