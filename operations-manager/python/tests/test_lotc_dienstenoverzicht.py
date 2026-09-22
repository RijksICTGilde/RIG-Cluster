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
from markupsafe import escape
from opi.api.invite_routes import invite_router
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType
from opi.web.lotc_router import router as lotc_router
from opi.web.navigation_lotc import to_nldd_icon
from starlette.middleware.sessions import SessionMiddleware
from tests.test_lotc_icon_mapping import _gerenderde_naam

#: Het icoon van een kaart staat VOOR zijn kop, in dezelfde c-cluster. Als paar gelezen,
#: want los geteld zegt een icoon niets over de kaart waar het bij hoort.
KAARTKOP = re.compile(r'<nldd-icon[^>]*name="([^"]+)"[^>]*>\s*</nldd-icon>\s*<nldd-title[^>]*>\s*<h3>([^<]+)</h3>')


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


def _zichtbaar_in_de_registry() -> list[ServiceType]:
    """De diensten die de pagina hoort te tonen, uit dezelfde bron als de pagina zelf.

    Afgeleid en niet overgetypt: een lijst met de hand erin zou meegroeien noch
    meekrimpen met de registry, en dan bewaakt hij alleen zichzelf.
    """
    return [
        service_type
        for service_type in ServiceAdapter.get_all_services()
        if not ServiceAdapter.SERVICE_DEFINITIONS[service_type].hidden
    ]


def _kaart(tekst: str, label: str) -> str:
    """Het stuk antwoord van de kop van deze dienst tot de kop van de volgende.

    Zonder die knip meet een assert alleen dat iets ERGENS op de pagina staat, en dan
    blijft een pagina die elke kaart dezelfde omschrijving geeft groen (gemeten).
    """
    for stuk in tekst.split("<h3>")[1:]:
        if stuk.startswith(f"{label}</h3>"):
            return stuk
    raise AssertionError(f"geen kaart met de kop {label!r}")


def test_er_staat_een_kaart_voor_elke_zichtbare_dienst(client: TestClient) -> None:
    """Zonder deze telling is elke toets hieronder gratis waar op een lege pagina."""
    verwacht = [_label(service_type) for service_type in _zichtbaar_in_de_registry()]
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


def test_elke_kaart_noemt_zijn_eigen_omschrijving_en_api_naam(client: TestClient) -> None:
    """De twee dingen waarvoor je deze pagina opent: wat doet het en hoe heet het.

    Binnen de kaart gemeten: met "staat ergens op de pagina" blijft deze toets groen als
    elke kaart de omschrijving van Keycloak draagt (gemeten).

    De API-naam staat in een ``<code>``, want die typ je letterlijk over in je
    projectbestand.
    """
    tekst = client.get("/lotc/bg/services").text

    for service_type in _zichtbaar_in_de_registry():
        definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]
        kaart = _kaart(tekst, definition.name)

        # Door escape heen, want een apostrof in de omschrijving komt er als &#39; uit
        # (Resource tuning heeft er een).
        assert str(escape(definition.description)) in kaart, f"{definition.name} mist zijn omschrijving"
        api_namen = [naam.strip() for naam in re.findall(r"<code[^>]*>([^<]+)</code>", kaart)]
        assert service_type.value in api_namen, f"{definition.name} noemt zijn API-naam niet: {api_namen}"


def test_elke_kaart_draagt_het_icoon_van_zijn_eigen_dienst(client: TestClient) -> None:
    """Niet een vast kaarticoon: elke dienst brengt zijn eigen beeld mee.

    Icoon en kop worden als PAAR gelezen. Los geteld blijft een pagina waarop elke kaart
    hetzelfde icoon draagt groen (gemeten), en bovendien staan er negen iconen van de
    pagina zelf voor de eerste kaart.

    Verwacht wordt de naam die de BROWSER opzoekt, dus na alle vertaalstappen: onze
    iconen dragen ROOS-namen, ``to_nldd_icon`` maakt van ``sleutel`` een ``lock-closed``,
    en de aliaslaag van LOTC herschrijft er daarna nog een paar (``database`` wordt
    ``cylinder-split``).
    """
    tekst = client.get("/lotc/bg/services").text
    definities = [ServiceAdapter.SERVICE_DEFINITIONS[service_type] for service_type in _zichtbaar_in_de_registry()]
    verwacht = [(_gerenderde_naam(to_nldd_icon(d.icon)), d.name) for d in definities]

    # Eén paar letterlijk, want de verwachting hierboven komt uit dezelfde vertaling als
    # de pagina: zonder dit blijft een pagina die helemaal niet meer vertaalt groen.
    assert ("lock-closed", _label(ServiceType.KEYCLOAK)) in verwacht

    assert KAARTKOP.findall(tekst) == verwacht


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
