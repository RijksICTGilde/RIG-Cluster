"""Het tabblad TOEGANG: de blokken die de diensten leveren staan op een eigen plek (RC-101).

Drie diensten leveren een blok voor de projectpagina - Keycloak (realm, admin-console,
gebruikersnaam, wachtwoord, gedeelde OTP), uitnodigingen (de link waarmee je iemand
binnenlaat) en bijlagen (de geuploade bestanden die een component meekrijgt). Dat is wat je
nodig hebt om een dienst te GEBRUIKEN, en het stond onderaan Overzicht tussen de rest.

Wat hier bewaakt wordt:

1. het tabblad bestaat, heeft een eigen pad en een route die dat pad bedient;
2. een project waarvan geen enkele dienst iets levert krijgt GEEN leeg tabblad;
3. de tabbalk laat een leeg tabblad weg in plaats van naar een lege pagina te wijzen.

Dat de blokken ook echt op dat tabblad staan (en niet meer op Overzicht) wordt op de
DRAAIENDE pagina gemeten: tests/e2e/test_lotc_toegang_tabblad.py. Dit bestand meet de
regels eromheen.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.web.lotc_switch import (
    PROJECT_TABS,
    STANDAARD_TAB,
    TABS_MET_VOORWAARDE,
    build_lotc_project_details,
    project_tab_url,
    tab_from_path,
)
from opi.web.router import web_router
from starlette.datastructures import URL


class _Verzoek:
    """Een verzoek zoals ``build_lotc_project_details`` het leest: alleen het pad en de
    querystring. De echte Request meebrengen zou een ASGI-scope vragen voor twee velden."""

    def __init__(self, pad: str) -> None:
        self.url = URL(pad)
        self.query_params: dict[str, str] = {}


def _tabbalk(pad: str, lege_tabs: tuple[str, ...] = ()) -> dict[str, Any]:
    gegevens = build_lotc_project_details(
        _Verzoek(pad),  # type: ignore[arg-type]
        user=None,
        project={"name": "demo", "deployments": []},
        lege_tabs=lege_tabs,
    )
    return gegevens["tabs"]


def test_services_info_is_een_eigen_tabblad_met_een_eigen_pad() -> None:
    assert PROJECT_TABS["services-info"]["label"] == "Services info"
    assert project_tab_url("demo", "services-info") == "/projects/demo/services-info"
    assert tab_from_path("/projects/demo/services-info") == "services-info"


def test_services_info_staat_naast_services_en_niet_erin() -> None:
    """Services gaat over beheer (wat staat er aan), Services info over wat een dienst zelf meldt (hoe kom je
    erbij). Twee tabbladen, dus ook twee adressen."""
    assert project_tab_url("demo", "services-info") != project_tab_url("demo", "services")


def test_het_lege_tabblad_valt_uit_de_tabbalk() -> None:
    """Een tab die een lege pagina opent is een belofte die niet waargemaakt wordt."""
    balk = _tabbalk("/projects/demo/details", lege_tabs=("services-info",))

    assert "services-info" not in balk
    assert "services" in balk, "alleen het lege tabblad hoort weg te vallen"


def test_zonder_lege_tabbladen_staat_toegang_er_gewoon() -> None:
    balk = _tabbalk("/projects/demo/details")

    assert "services-info" in balk
    assert balk["services-info"]["url"] == "/projects/demo/services-info"


def test_alleen_toegang_kan_wegvallen() -> None:
    """De voorwaardelijke tabbladen staan op EEN plek, zodat de route en de tabbalk het
    over dezelfde regel hebben."""
    assert TABS_MET_VOORWAARDE == ("services-info",)
    assert all(tab in PROJECT_TABS for tab in TABS_MET_VOORWAARDE)
    assert STANDAARD_TAB not in TABS_MET_VOORWAARDE, "het terugvaltabblad mag nooit wegvallen"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(web_router)
    return TestClient(app)


def test_de_oude_vorm_van_het_adres_verwijst_door(client: TestClient) -> None:
    """Beide vormen staan geregistreerd, net als bij de andere tabbladen."""
    antwoord = client.get("/projects/services-info/demo", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/projects/demo/services-info"
