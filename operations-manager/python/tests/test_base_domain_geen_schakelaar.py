"""``__custom__`` is een schakelaar in het formulier, dus geen waarde in de API.

DE MELDING (zad-cli)

De keuzelijst voor ``base-domain`` bood ``__custom__`` aan; elke schrijfactie met die
waarde werd geweigerd. Een eigen domein als vrije tekst invullen werkte wel, maar dat stond
nergens.

WAT ER WAS

``__custom__`` betekent in het formulier "ik vul zelf een domein in": de select schakelt
dan een tweede, TIJDELIJK veld aan (``deployments[*]/base-domain:custom``) waar het echte
domein in gaat. Dat tweede veld bestaat alleen in het formulier -- het configmodel kent het
niet en weigert het (``extra="forbid"``) -- dus een API-client kan de schakelaar wel zetten
maar nooit invullen. ``DomainConfigEnforcer`` weigert die stand terecht met "Een aangepast
domein is geselecteerd maar niet ingevuld". Ondertussen publiceerde de API de waarde op
twee plekken als keuze: in ``GET /projects/{p}/clusters`` en in de ``x-choices-source`` van
het veld zelf.

DE KEUZE

Weg uit wat de API publiceert, en het formulier houdt hem. Een keuzelijst die een waarde
noemt die de uitrol weigert is dezelfde klasse fout als bij ``domain-format``. Een eigen
domein zet je door het domein ZELF in ``base-domain`` te zetten, en dat staat nu in de
beschrijving van het veld en van de lijst.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.forms.visualizers.providers import (
    CUSTOM_DOMAIN_SENTINEL,
    BaseDomainOptionsProvider,
    ClusterBaseDomainOptionsProvider,
)
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


class TestDeApiPubliceertHemNietMeer:
    def test_de_clusterlijst_bevat_de_schakelaar_niet(self, client: TestClient) -> None:
        """Het endpoint waar ``x-choices-source`` naar verwijst is de keuzelijst zelf."""
        response = client.get(f"/api/v2/projects/{PROJECT}/clusters", headers=HEADERS)

        assert response.status_code == 200
        waarden = [optie["value"] for cluster in response.json()["clusters"] for optie in cluster["base-domains"]]
        assert waarden, "zonder domeinen zegt deze test niets"
        assert CUSTOM_DOMAIN_SENTINEL not in waarden

    def test_de_keuzebron_van_het_veld_noemt_hem_niet(self, spec: dict[str, Any]) -> None:
        veld = spec["components"]["schemas"]["PublishOnWebDeploymentConfig"]["properties"]["base-domain"]
        bron = veld["x-choices-source"]

        assert CUSTOM_DOMAIN_SENTINEL not in bron["description"]

    def test_de_beschrijvingen_zeggen_hoe_een_eigen_domein_dan_wel_moet(self, spec: dict[str, Any]) -> None:
        """Weghalen zonder het alternatief te noemen laat de lezer met niets achter."""
        veld = spec["components"]["schemas"]["PublishOnWebDeploymentConfig"]["properties"]["base-domain"]

        assert "eigen domein" in veld["x-choices-source"]["description"].lower()

    def test_de_clusterlijst_beschrijft_zichzelf_zonder_de_schakelaar(self, spec: dict[str, Any]) -> None:
        optie = spec["components"]["schemas"]["ClusterDomainOption"]["properties"]["value"]

        assert CUSTOM_DOMAIN_SENTINEL not in optie["description"]


class TestHetFormulierHoudtHem:
    """De schakelaar is daar geen fout maar de hele bedoeling."""

    def test_de_clusterprovider_biedt_hem_nog_aan(self) -> None:
        waarden = [optie["value"] for optie in ClusterBaseDomainOptionsProvider().get_options()]

        assert CUSTOM_DOMAIN_SENTINEL in waarden

    def test_de_vaste_provider_biedt_hem_nog_aan(self) -> None:
        waarden = [optie["value"] for optie in BaseDomainOptionsProvider().get_options()]

        assert CUSTOM_DOMAIN_SENTINEL in waarden


class TestWaaromHijNietGepubliceerdMagWorden:
    async def test_de_schrijfactie_weigert_de_schakelaar(self) -> None:
        """De reden dat dit uit de keuzelijst moet: een client die hem stuurt loopt vast.

        Een API-client kan het tijdelijke formulierveld niet vullen, dus deze stand is voor
        hem onontkoombaar.
        """
        from opi.forms.editables.enforcers import DomainConfigEnforcer

        project = {
            "name": PROJECT,
            "deployments": [
                {
                    "name": "productie",
                    "base-domain": CUSTOM_DOMAIN_SENTINEL,
                    "domain-format": "component-deployment-project",
                }
            ],
        }

        with pytest.raises(ValueError, match="niet ingevuld"):
            await DomainConfigEnforcer().enforce(project, {"project_name": PROJECT})
