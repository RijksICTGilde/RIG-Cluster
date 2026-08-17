"""De clusterlijst zegt welk domein zonder goedkeuring in gebruik gaat.

DE MELDING (zad-cli)

"``base-domains`` geeft ``value`` en ``label``; een domein eruit kiezen levert alsnog een
approval op. Een veld dat de twee gevallen scheidt zetten wij in de keuzelijst."

WAT ER WAS

De regel staat in ``is_deployment_domain_approved``: een leeg ``base-domain`` of het domein
van het cluster zelf gaat meteen in gebruik, elke andere waarde moet in de goedkeuringslijst
van het project staan. Het domein van het cluster stond nergens als veld in het antwoord --
alleen als vrije tekst binnen het label van de lege optie ("Cluster standaard (…)"). En de
naïeve regel "alleen de lege waarde is gratis" is aantoonbaar fout: op sandboxed-local is
``sandbox.rijksapp.dev`` zowel het clusterdomein als een entry in de lijst, dus die entry
vraagt geen goedkeuring terwijl hij er identiek uitziet als een die dat wel doet.

DE KEUZE

Het feit erbij, niet het oordeel: ``default-domain`` is het domein van het cluster, en de
client vergelijkt. Een veld dat per optie "heeft dit goedkeuring nodig" zegt, kan óók de
stand van dit project meewegen (een al goedgekeurd domein vraagt niets meer), en dat is een
semantiekkeuze die de eigenaar hoort te maken. Dit veld kan niet verkeerd zijn en sluit die
rijkere variant niet uit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.connectors.subdomain import is_deployment_domain_approved
from opi.core.cluster_config import get_ingress_postfix
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

if TYPE_CHECKING:
    from fastapi import FastAPI

PROJECT = "demo"
API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}

PROJECT_DATA: dict[str, Any] = {
    "schema-version": 2,
    "name": PROJECT,
    "clusters": ["local"],
    "users": [{"email": "user@example.com", "role": "admin"}],
    "config": {"age-public-key": "age1notarealkey"},
}


@pytest.fixture
def client(mock_settings: Any) -> TestClient:
    from opi.server import create_app

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
        app: FastAPI = create_app()
        yield TestClient(app)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    from opi.server import app

    return app.openapi()


def _clusters(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(f"/api/v2/projects/{PROJECT}/clusters", headers=HEADERS)
    assert response.status_code == 200
    clusters = response.json()["clusters"]
    assert clusters, "zonder clusters zegt deze test niets"
    return clusters


class TestHetVeldStaatErEnKlopt:
    def test_elk_cluster_noemt_zijn_eigen_domein(self, client: TestClient) -> None:
        for cluster in _clusters(client):
            verwacht = get_ingress_postfix(cluster["name"]).lstrip(".")

            assert cluster["default-domain"] == verwacht

    def test_het_domein_draagt_geen_punt_voorop(self, client: TestClient) -> None:
        """De postfix is intern '.domein'; als base-domain-waarde is die punt fout."""
        for cluster in _clusters(client):
            assert not cluster["default-domain"].startswith(".")

    def test_dat_domein_is_precies_wat_zonder_goedkeuring_mag(self, client: TestClient) -> None:
        """De poort zelf, niet een tweede beschrijving ervan."""
        for cluster in _clusters(client):
            assert is_deployment_domain_approved(PROJECT_DATA, cluster["default-domain"], None, cluster["name"])

    def test_een_ander_domein_uit_de_lijst_vraagt_wel_goedkeuring(self, client: TestClient) -> None:
        """Het geval dat de melder maakte: een keuze uit de lijst die alsnog wacht."""
        gemeten = False
        for cluster in _clusters(client):
            for optie in cluster["base-domains"]:
                if not optie["value"] or optie["value"] == cluster["default-domain"]:
                    continue
                gemeten = True
                assert not is_deployment_domain_approved(PROJECT_DATA, optie["value"], None, cluster["name"])
        assert gemeten, "geen enkel cluster bood een domein naast zijn eigen domein aan"


class TestDeSpecLegtHetUit:
    def test_het_veld_staat_in_het_openapi_document(self, spec: dict[str, Any]) -> None:
        eigenschappen = spec["components"]["schemas"]["ClusterInfo"]["properties"]

        assert "default-domain" in eigenschappen

    def test_de_beschrijving_zegt_waar_het_veld_voor_dient(self, spec: dict[str, Any]) -> None:
        """Een kaal domein zonder uitleg laat de lezer de regel raden."""
        beschrijving = spec["components"]["schemas"]["ClusterInfo"]["properties"]["default-domain"]["description"]

        assert "goedkeuring" in beschrijving.lower()
