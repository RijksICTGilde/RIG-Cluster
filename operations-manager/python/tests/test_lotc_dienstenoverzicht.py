"""Het dienstenoverzicht toont de diensten die de registry werkelijk aanbiedt.

``lotc_fixtures.services_overview()`` bouwt de rij die ``bg/services.html.j2`` per dienst
rendert. In de standaardsuite keek niets naar de INHOUD van dat antwoord:
``tests/test_lotc_geen_roos_html_in_het_antwoord.py`` eist status 200 en geen rvo-klassen,
en dat is ook waar op een pagina zonder een enkele kaart. Dat een kaart er staat, stond
alleen in ``tests/e2e/test_lotc_iconen_tekenen.py``, en die draait niet mee in de
standaardrun.

Gemeten op ``/lotc/bg/services``: dezelfde sjablonen met de voorbeeldprojecten uit
``opi/web/lotc_fixtures/``, dus dezelfde pagina met andere gegevens.

Wat hier bewust NIET staat: een toets op ``kind_type``, ``help``, ``used_by`` of
``service_filters``. Die worden door geen enkel sjabloon gelezen, net als de ``chips`` die
RC-215 weghaalde, en vastleggen wat niemand rendert is precies wat die taak opruimde.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    return TestClient(app)


def _dienstnamen(client: TestClient, query: str = "") -> list[str]:
    antwoord = client.get(f"/lotc/bg/services{query}")
    assert antwoord.status_code == 200, f"/lotc/bg/services{query} rendert niet"
    return re.findall(r"<h3>([^<]+)</h3>", antwoord.text)


def _label(service_type: ServiceType) -> str:
    return ServiceAdapter.SERVICE_DEFINITIONS[service_type].name


def _zichtbaar_in_de_registry() -> list[str]:
    """De labels die de pagina hoort te tonen, uit dezelfde bron als de pagina zelf.

    Afgeleid en niet overgetypt: een lijst met de hand erin zou meegroeien noch
    meekrimpen met de registry, en dan bewaakt hij alleen zichzelf.
    """
    return [
        ServiceAdapter.SERVICE_DEFINITIONS[service_type].name
        for service_type in ServiceAdapter.get_all_services()
        if not getattr(ServiceAdapter.SERVICE_DEFINITIONS[service_type], "hidden", False)
    ]


def test_er_staat_een_kaart_voor_elke_zichtbare_dienst(client: TestClient) -> None:
    """Zonder deze telling is elke toets hieronder gratis waar op een lege pagina."""
    verwacht = _zichtbaar_in_de_registry()
    assert len(verwacht) > 10, f"te weinig diensten verzameld ({len(verwacht)}); deze toets zou niets meten"
    assert _dienstnamen(client) == verwacht


def test_een_verborgen_dienst_krijgt_geen_kaart(client: TestClient) -> None:
    """``hidden`` is de enige reden waarom een dienst van deze pagina wegblijft.

    De naam is een deelreeks van een dienst die er WEL op staat ("PostgreSQL Database"),
    dus de vergelijking gaat over de hele kop en niet over een stuk van de pagina.
    """
    namen = _dienstnamen(client)
    assert _label(ServiceType.NAMESPACE_POSTGRESQL_DATABASE) not in namen
    assert _label(ServiceType.POSTGRESQL_DATABASE) in namen


def test_een_kaart_noemt_de_omschrijving_en_de_api_naam(client: TestClient) -> None:
    """De twee dingen waarvoor je deze pagina opent: wat doet het en hoe heet het.

    De API-naam staat in een ``<code>``, want die typ je letterlijk over in je
    projectbestand.
    """
    tekst = client.get("/lotc/bg/services").text
    definition = ServiceAdapter.SERVICE_DEFINITIONS[ServiceType.KEYCLOAK]

    assert definition.description in tekst
    api_namen = [naam.strip() for naam in re.findall(r"<code[^>]*>([^<]+)</code>", tekst)]
    assert ServiceType.KEYCLOAK.value in api_namen


def test_elke_kaart_draagt_het_icoon_van_zijn_eigen_dienst(client: TestClient) -> None:
    """Niet een vast kaarticoon: elke dienst brengt zijn eigen beeld mee.

    Gemeten op de naam zoals hij in het ANTWOORD staat, want onze iconen dragen
    ROOS-namen en NLDD kent alleen zijn eigen woordenschat: Keycloak declareert
    ``sleutel`` en dat hoort er als ``lock-closed`` uit te komen.

    Welke van de twee vertaalstappen dat doet, meet deze toets niet: het sjabloon zet er
    nog een ``| nldd_icon`` overheen, dus zonder de aanroep in ``services_overview`` blijft
    hij groen (gemeten). Rood wordt hij als de sleutel uit de rij valt of als elke dienst
    hetzelfde icoon krijgt.
    """
    tekst = client.get("/lotc/bg/services").text
    iconen = re.findall(r"<nldd-icon[^>]*name=\"([^\"]+)\"", tekst)

    assert ServiceAdapter.SERVICE_DEFINITIONS[ServiceType.KEYCLOAK].icon == "sleutel"
    assert "lock-closed" in iconen, f"het Keycloak-icoon staat er niet: {sorted(set(iconen))}"


def test_het_filter_splitst_de_lijst_in_zelf_te_kiezen_en_altijd_aan(client: TestClient) -> None:
    """``?kind=`` is een echte link: de keuze staat in de URL en werkt zonder JavaScript.

    De twee helften samen zijn de hele lijst, en ze overlappen niet. Dat is wat er
    kapotgaat als ``kind_label`` uit de rij verdwijnt: de filter valt dan stil terug op
    "alles" in plaats van een foutmelding te geven.
    """
    alle = _dienstnamen(client)
    zelf = _dienstnamen(client, "?kind=user")
    altijd = _dienstnamen(client, "?kind=system")

    assert zelf, "de helft 'zelf te kiezen' is leeg; dan scheidt het filter niets"
    assert altijd, "de helft 'altijd aan' is leeg; dan scheidt het filter niets"
    assert (set(zelf) | set(altijd)) == set(alle), "samen zijn de twee helften niet de hele lijst"
    assert not (set(zelf) & set(altijd)), f"deze diensten staan in beide helften: {sorted(set(zelf) & set(altijd))}"
    assert _label(ServiceType.PLATFORM) in altijd
    assert _label(ServiceType.KEYCLOAK) in zelf


def test_een_onbekende_filterwaarde_toont_de_hele_lijst(client: TestClient) -> None:
    """Een verkeerd gedeelde link hoort de pagina te tonen, niet stuk te gaan."""
    assert _dienstnamen(client, "?kind=bestaatniet") == _dienstnamen(client)
