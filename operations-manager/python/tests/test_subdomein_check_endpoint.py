"""De subdomeincontrole vraagt om een projectnaam, dus staat de projectnaam in het pad.

DE MELDING (zad-cli)

``GET /api/subdomains/check/{sub}?base_domain=...`` antwoordde met
``401 "Missing project_name parameter"``. Die parameter stond niet in de OpenAPI-spec, en
als queryparameter meesturen hielp niet: FastAPI geeft een route alleen de parameters die
in haar signatuur staan, dus ``project_name`` kwam nooit in de kwargs terecht. Het endpoint
was daarmee voor iedereen onbereikbaar -- niet soms, altijd.

DE OORZAAK

``validate_api_token`` legitimeert een projectsleutel TEGEN een project, en leest dat
project uit de kwargs van de route. Een route zonder ``project_name`` kan die controle niet
doorstaan. Het was dus geen fout van de aanroeper maar van de route.

DE KEUZE

De controle hoort bij een project: je vraagt een subdomein op omdat je het voor jouw
project wilt claimen, de reservering wordt per project/deployment vastgelegd, en de
decorator eraf halen zou het endpoint anoniem maken -- precies de enumeratie waartegen hij
er zat. Dus verhuist hij naar de vorm die de rest van v2 heeft: de projectnaam in het PAD.

DE VERGRENDELING

De laatste test hieronder is generiek: elke route die ``validate_api_token`` draagt, moet
een VERPLICHTE ``project_name`` kunnen ontvangen (in haar pad of als queryparameter). Dat
vond nog twee gevallen: ``GET /api/v1/backup/status`` had de naam helemaal niet en gaat naar
de mastersleutel, en ``GET /api/subdomains`` had hem als optionele filter terwijl weglaten
dezelfde 401 opleverde.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.api.endpoint_util import REQUIRES_PROJECT_NAME
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

if TYPE_CHECKING:
    from fastapi import FastAPI

PROJECT = "demo"
API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}

PATH = f"/api/v2/projects/{PROJECT}/subdomains/check/mijnapp"

PROJECT_DATA: dict[str, Any] = {
    "schema-version": 2,
    "name": PROJECT,
    "clusters": ["local"],
    "users": [{"email": "user@example.com", "role": "admin"}],
    "config": {"age-public-key": "age1notarealkey"},
}


@pytest.fixture
def store() -> Any:
    mock_service = MagicMock(spec=GitProjectStore)
    stored = ProjectSummary(
        name=PROJECT,
        api_key=API_KEY,
        filename=f"{PROJECT}.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=PROJECT_DATA,
    )
    mock_service.get = lambda name: stored if name == PROJECT else None
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.v2.router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def connector() -> Any:
    """De registerlezer: standaard is het subdomein vrij."""
    subdomain_connector = MagicMock()
    subdomain_connector.check_availability = AsyncMock(return_value=True)
    with patch("opi.api.v2.router.create_subdomain_connector", return_value=subdomain_connector):
        yield subdomain_connector


@pytest.fixture
def client(mock_settings: Any, store: Any, connector: Any) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    return TestClient(app)


class TestDeRouteIsBereikbaar:
    def test_met_de_projectsleutel_komt_er_een_antwoord(self, client: TestClient) -> None:
        """Het geval uit de melding: dit gaf altijd 401, wat de route ook meestuurde."""
        response = client.get(PATH, params={"base_domain": "rijksapp.nl"}, headers=HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["subdomain"] == "mijnapp"
        assert body["base_domain"] == "rijksapp.nl"
        assert body["available"] is True

    def test_een_bezet_subdomein_meldt_zich_als_bezet(self, client: TestClient, connector: Any) -> None:
        connector.check_availability = AsyncMock(return_value=False)

        response = client.get(PATH, params={"base_domain": "rijksapp.nl"}, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["available"] is False

    def test_een_ongeldig_subdomein_wordt_benoemd_en_niet_opgezocht(self, client: TestClient, connector: Any) -> None:
        response = client.get(
            f"/api/v2/projects/{PROJECT}/subdomains/check/Niet Geldig",
            params={"base_domain": "rijksapp.nl"},
            headers=HEADERS,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert body["validation_error"]
        connector.check_availability.assert_not_awaited()


class TestDeControleBlijftEenControle:
    """De decorator eraf halen was de andere uitweg, en die kost de afscherming."""

    def test_zonder_sleutel_geen_antwoord(self, client: TestClient) -> None:
        response = client.get(PATH, params={"base_domain": "rijksapp.nl"})

        assert response.status_code == 401

    def test_de_sleutel_van_een_ander_project_telt_niet(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/ander-project/subdomains/check/mijnapp",
            params={"base_domain": "rijksapp.nl"},
            headers=HEADERS,
        )

        assert response.status_code == 401


class TestHetDocumentZegtHetOok:
    """Een client leest de spec; die moet de projectnaam noemen, anders blijft het raden."""

    @pytest.fixture(scope="class")
    def spec(self) -> dict[str, Any]:
        from opi.server import app

        return app.openapi()

    def test_het_pad_staat_in_het_document_met_de_projectnaam(self, spec: dict[str, Any]) -> None:
        operation = spec["paths"]["/api/v2/projects/{project_name}/subdomains/check/{subdomain}"]["get"]
        parameters = {parameter["name"]: parameter["in"] for parameter in operation["parameters"]}

        assert parameters["project_name"] == "path"
        assert parameters["subdomain"] == "path"
        assert parameters["base_domain"] == "query"

    def test_de_oude_route_belooft_niets_meer(self, spec: dict[str, Any]) -> None:
        """Hij kon niet slagen, dus er is geen client die hem kwijtraakt."""
        assert "/api/subdomains/check/{subdomain}" not in spec["paths"]

    def test_elke_projectsleutelroute_kan_een_projectnaam_ontvangen(self, spec: dict[str, Any]) -> None:
        """De vergrendeling, uit de routes zelf en niet uit een lijst met paden.

        Twee eisen, en beide zijn een keer misgegaan. De naam moet er ZIJN, anders is elk
        antwoord een 401 (de melding, en ``GET /api/v1/backup/status`` had hem ook niet). En
        hij moet VERPLICHT zijn, anders belooft het document een optionele parameter waarvan
        het weglaten alsnog 401 oplevert -- dat was ``GET /api/subdomains``.
        """
        from opi.server import app

        gebrekkig: list[str] = []
        for route in app.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None or not getattr(endpoint, REQUIRES_PROJECT_NAME, False):
                continue
            waar = f"{sorted(getattr(route, 'methods', None) or [])} {route.path}"
            parameter = inspect.signature(endpoint).parameters.get("project_name")
            if parameter is None:
                gebrekkig.append(f"{waar} (geen project_name)")
            elif parameter.default is None:
                gebrekkig.append(f"{waar} (project_name is optioneel)")

        assert not gebrekkig, (
            "deze routes worden met een projectsleutel afgeschermd maar kunnen geen project "
            f"aanwijzen om die sleutel tegen te controleren: {gebrekkig}"
        )
