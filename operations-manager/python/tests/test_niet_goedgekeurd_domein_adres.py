"""Het adres dat getoond wordt is het adres dat bediend wordt, en anders staat erbij waarom.

Project ``cmt2-om5`` vroeg een eigen domein aan dat nooit is goedgekeurd, en de portal
toonde ``https://frontend.rig-test.mijn-webshop-test.nl/`` als HET adres van de applicatie.
Twee waarheden over dezelfde URL, en de gebruiker kreeg de verkeerde te zien.

De oorzaak zat in ``get_component_ingress_map``: ``apply_domain_approval_fallback`` stond
binnen de tak die alleen wordt gelopen wanneer de deployment een ``domain-format`` noemt.
Een deployment zonder dat veld -- de oude ``domain-mode: nice-url``, of een schrijfactie die
alleen ``base-domain`` en ``subdomain`` zet -- viel in de dispatch daaronder, waar het
domein zonder enige controle in de hostnaam werd gezet. Dat is EEN functie, en die voedt
zowel de ingress als de portal en de API, dus het adres klopte nergens.

Deze tests meten beide kanten van dezelfde vraag:

* het ADRES: een niet-goedgekeurd domein levert het veilige clusteradres, in elke
  opslagvorm, en een goedgekeurd domein levert gewoon het eigen domein;
* de WAARSCHUWING: het leesantwoord noemt de aanvraag met de juiste van de drie statussen
  (``none``, ``requested``, ``denied``), want op afgewezen ga je niet zitten wachten.

De waarschuwing komt uit de bestaande goedkeuringsterugkoppeling
(``collect_deployment_approval_notices`` -> ``approvals``); er is hier geen tweede
mechanisme voor gebouwd.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.publish_on_web.urls import public_urls_for_deployment
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI

API_KEY = "test-api-key-12345"
PROJECT = "cmt2-om5"
PUBLISH_ON_WEB = ServiceType.PUBLISH_ON_WEB.value

#: Het domein uit de melding: van dit project, nooit goedgekeurd.
EIGEN_DOMEIN = "mijn-webshop-test.nl"
SUBDOMEIN = "rig-test"

#: Het clusterdomein, vastgezet zodat het verwachte terugvaladres letterlijk uitgeschreven
#: kan worden. Een niet-goedgekeurd domein valt terug op het veilige formaat
#: ``component-deployment-project`` hierop.
CLUSTERPOSTFIX = ".local.test"
CLUSTERADRES = f"https://frontend-{SUBDOMEIN}-{PROJECT}{CLUSTERPOSTFIX}"

#: De twee opslagvormen van hetzelfde webadres. De eerste is die uit de melding: geen
#: ``domain-format``, alleen de oude ``domain-mode``. Precies die vorm liep om de poort
#: heen, dus een test die alleen de tweede meet was groen op een kapot systeem.
VORMEN: dict[str, dict[str, Any]] = {
    "zonder-domain-format": {
        "base-domain": EIGEN_DOMEIN,
        "subdomain": SUBDOMEIN,
        "domain-mode": "nice-url",
    },
    "met-domain-format": {
        "base-domain": EIGEN_DOMEIN,
        "subdomain": SUBDOMEIN,
        "domain-format": "component.subdomain",
    },
}

#: Het adres dat het project VRAAGT, in beide vormen hetzelfde.
GEVRAAGD_ADRES = f"frontend.{SUBDOMEIN}.{EIGEN_DOMEIN}"


def _project(webconfig: dict[str, Any], domeinstatus: str | None) -> dict[str, Any]:
    """Een project met een deployment op ``webconfig``.

    ``domeinstatus`` is None wanneer er helemaal geen aanvraag op naam van het domein
    staat -- dat is de derde van de drie gevallen, en niet hetzelfde als afgewezen.
    """
    project: dict[str, Any] = {
        "name": PROJECT,
        "clusters": ["local"],
        "components": [{"name": "frontend", "services": [PUBLISH_ON_WEB], "ports": {"inbound": [8080]}}],
        "deployments": [
            {
                "name": SUBDOMEIN,
                "cluster": "local",
                "namespace": PROJECT,
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
                "services": [{"reference": PUBLISH_ON_WEB, "config": dict(webconfig)}],
            }
        ],
    }
    if domeinstatus is not None:
        project["services"] = [
            {
                "reference": PUBLISH_ON_WEB,
                "config": {
                    "domains": {
                        "allowed-domains": [
                            {
                                "domain": EIGEN_DOMEIN,
                                "status": domeinstatus,
                                "history": [
                                    {
                                        "date": "2026-08-01T00:00:00+00:00",
                                        "status": domeinstatus,
                                        "by": "beheerder@example.nl",
                                        "message": "toelichting van de beheerder",
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        ]
    return project


# ---------------------------------------------------------------------------
# Het adres, op de weg die de portal leest
# ---------------------------------------------------------------------------


@pytest.fixture
def clusteradres() -> Iterator[None]:
    """Vast clusterdomein, zodat het verwachte terugvaladres letterlijk uitgeschreven is."""
    with (
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_postfix", return_value=CLUSTERPOSTFIX),
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_tls_enabled", return_value=True),
    ):
        yield


def _adressen(project_data: dict[str, Any]) -> list[str]:
    deployment = project_data["deployments"][0]
    return [link["url"] for link in public_urls_for_deployment(project_data, deployment, PROJECT, ProjectFileHandler())]


@pytest.mark.parametrize("vorm", list(VORMEN))
@pytest.mark.parametrize("domeinstatus", [None, "requested", "denied"])
def test_de_portal_toont_het_clusteradres_zolang_het_domein_niet_is_goedgekeurd(
    clusteradres: None, vorm: str, domeinstatus: str | None
) -> None:
    """Geen aanvraag, een lopende aanvraag en een afgewezen aanvraag zijn alle drie
    "niet goedgekeurd", en leveren dus geen van drieën het gevraagde adres op."""
    adressen = _adressen(_project(VORMEN[vorm], domeinstatus))

    assert adressen == [CLUSTERADRES]
    assert GEVRAAGD_ADRES not in " ".join(adressen)


@pytest.mark.parametrize("vorm", list(VORMEN))
def test_een_goedgekeurd_domein_levert_gewoon_het_eigen_adres(clusteradres: None, vorm: str) -> None:
    """De negatieve kant: de poort mag niet zomaar alles naar het cluster trekken."""
    assert _adressen(_project(VORMEN[vorm], "approved")) == [f"https://{GEVRAAGD_ADRES}"]


# ---------------------------------------------------------------------------
# Het adres en de waarschuwing, op de weg die de API leest
# ---------------------------------------------------------------------------


@pytest.fixture
def api(mock_settings: Any, request: pytest.FixtureRequest) -> Iterator[TestClient]:
    """Een TestClient waarvan het project uit ``request.param`` komt.

    De projectgegevens hangen aan de store, dus ze moeten voor de client vaststaan; elke
    test geeft zijn eigen project mee via indirect parametriseren.
    """
    project_data: dict[str, Any] = copy.deepcopy(request.param)
    store = MagicMock(spec=GitProjectStore)
    summary = ProjectSummary(
        name=PROJECT,
        api_key=API_KEY,
        filename=f"{PROJECT}.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=project_data,
    )
    store.get = lambda name: summary if name == PROJECT else None

    argo = MagicMock()
    argo.auth_token = "fake-token"
    argo.get_application_status = AsyncMock(return_value=None)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_namespace_events = AsyncMock(return_value=[])

    from opi.server import create_app

    app: FastAPI = create_app()
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.v2.router.get_project_store", return_value=store),
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_postfix", return_value=CLUSTERPOSTFIX),
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_tls_enabled", return_value=True),
        patch("opi.api.v2.router.create_argo_connector", return_value=argo),
        patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl),
        patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value=f"rig-{PROJECT}"),
    ):
        yield TestClient(app)


def _lees(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/api/v2/projects/{PROJECT}/deployments/{SUBDOMEIN}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("api", "verwachte_status", "verwachte_zin"),
    [
        (_project(VORMEN["zonder-domain-format"], None), "none", "is niet goedgekeurd"),
        (_project(VORMEN["zonder-domain-format"], "requested"), "requested", "wacht op goedkeuring"),
        (_project(VORMEN["zonder-domain-format"], "denied"), "denied", "is afgewezen"),
        (_project(VORMEN["met-domain-format"], None), "none", "is niet goedgekeurd"),
        (_project(VORMEN["met-domain-format"], "requested"), "requested", "wacht op goedkeuring"),
        (_project(VORMEN["met-domain-format"], "denied"), "denied", "is afgewezen"),
    ],
    indirect=["api"],
)
def test_de_api_meldt_het_clusteradres_met_de_juiste_van_de_drie_statussen(
    api: TestClient, verwachte_status: str, verwachte_zin: str
) -> None:
    """``urls`` en ``approvals`` moeten hetzelfde verhaal vertellen.

    Ze deden dat niet: ``approvals`` zei "bereikbaar op het standaard clusteradres" terwijl
    ``urls`` het gevraagde domein noemde. Een client die op ``urls`` afgaat kreeg een adres
    dat niets bedient.
    """
    detail = _lees(api)

    assert detail["urls"] == {"frontend": CLUSTERADRES}

    (melding,) = detail["approvals"]
    assert melding["service"] == PUBLISH_ON_WEB
    assert melding["type"] == "domain"
    assert melding["subject"] == EIGEN_DOMEIN
    assert melding["status"] == verwachte_status
    assert verwachte_zin in melding["text"]
    assert "clusteradres" in melding["text"]


@pytest.mark.parametrize(
    "api",
    [_project(VORMEN["zonder-domain-format"], "approved"), _project(VORMEN["met-domain-format"], "approved")],
    indirect=True,
)
def test_de_api_zwijgt_over_een_goedgekeurd_domein_en_noemt_het_eigen_adres(api: TestClient) -> None:
    detail = _lees(api)

    assert detail["urls"] == {"frontend": f"https://{GEVRAAGD_ADRES}"}
    assert detail["approvals"] == []
