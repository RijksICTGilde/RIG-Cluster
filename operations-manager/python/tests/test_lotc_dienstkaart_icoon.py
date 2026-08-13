"""De dienstkaarten op de projectpagina tonen het icoon van hun dienst.

Op ``/projects/services/<naam>`` toonde elke kaart de naam, het bindingslabel en de
omschrijving, en verder niets. Elke dienst DECLAREERT een icoon (``service_def.icon``);
het tabblad gebruikte het alleen nergens. Dat is geen icoon dat stukging maar een icoon
dat nooit gerenderd werd, en dat is precies het soort gat dat een test niet vangt zolang
hij naar de gegevens kijkt in plaats van naar het antwoord.

Vandaar de meting op het ANTWOORD van ``/lotc/bg/project-tabs?tab=services``: dezelfde
sjablonen met de voorbeeldprojecten uit ``opi/web/lotc_fixtures/``, dus dezelfde pagina
met andere gegevens.

Dat de gekozen naam ook echt een icoon OPLEVERT staat in
``tests/test_lotc_icon_mapping.py``; hier gaat het erom dat hij er staat.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    return TestClient(app)


def _diensttabblad(client: TestClient) -> str:
    antwoord = client.get("/lotc/bg/project-tabs?tab=services")
    assert antwoord.status_code == 200
    kop = antwoord.text.find("Services &amp; Integraties")
    assert kop != -1, "het diensttabblad rendert zijn eigen kop niet"
    return antwoord.text[kop:]


def _iconen(html: str) -> list[str]:
    return re.findall(r"<nldd-icon[^>]*name=\"([^\"]+)\"", html)


def _dienstnamen(html: str) -> list[str]:
    return re.findall(r"<h3>([^<]+)</h3>", html)


def test_de_proefopstelling_toont_dienstkaarten(client: TestClient) -> None:
    """Bewaak de bewaker: zonder kaarten is "elke kaart heeft een icoon" gratis waar."""
    assert len(_dienstnamen(_diensttabblad(client))) >= 3


def test_elke_dienstkaart_toont_een_icoon(client: TestClient) -> None:
    """Evenveel iconen als kaarten: geen enkele kaart blijft zonder."""
    html = _diensttabblad(client)
    assert len(_iconen(html)) >= len(_dienstnamen(html))


def test_het_icoon_is_dat_van_de_dienst_zelf(client: TestClient) -> None:
    """Niet een vast kaarticoon: elke dienst brengt zijn eigen beeld mee.

    Keycloak draagt ``sleutel`` (-> lock-closed) en publish-on-web ``wereldbol``
    (-> globe). Staat daar een en dezelfde naam, dan is het icoon versiering geworden.
    """
    iconen = _iconen(_diensttabblad(client))
    assert "lock-closed" in iconen, f"het Keycloak-icoon staat er niet: {iconen}"
    assert "globe" in iconen, f"het icoon van publiceren-op-het-web staat er niet: {iconen}"
