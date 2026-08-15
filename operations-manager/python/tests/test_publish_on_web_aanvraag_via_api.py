"""Domeinen en subdomeinen zijn op aanvraag -- ook wanneer de API ze zet.

De regel: de configlijst houdt de goedkeuringsstatus op PROJECTniveau en een API-call kan
daar nooit op muteren, maar via de API moet je op een deployment wel een domein, subdomein
en domain-format kunnen zetten, waarbij een nog niet goedgekeurd domein automatisch een
aanvraag wordt en dat ook wordt teruggekoppeld.

Wat er hiervoor gebeurde, en waarom deze tests bestaan:

* ``PUT .../services/publish-on-web/config/deployment/{d}`` schreef het domein weg en
  maakte GEEN aanvraag. De deployment publiceerde daarna op het standaard clusteradres
  (``apply_domain_approval_fallback``) en niets in het antwoord zei waarom. Het eerste
  signaal voor de client was een ingress die niet op het gevraagde adres verscheen.
* ``POST .../deployments`` deed het omgekeerde: ``_enforce_domain_config`` behandelde de
  ``FieldWarning`` "is op aanvraag" -- in de wizard niet-blokkerend -- als een harde
  afwijzing, waardoor de ``ensure_domain_requests`` twee regels verderop nooit werd
  bereikt. Een domein aanvragen via de API kon dus alleen maar mislukken.

De portal deed het al goed: een verplicht vinkje "Domein aanvragen" laat
``DomainRequestHook`` ``ensure_domain_requests`` aanroepen. Dat is precies de functie die
de API nu ook bereikt, via de catalogus, zodat het om dezelfde aanvraag gaat en niet om
een tweede mechanisme.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.connectors.subdomain import get_domains_config
from opi.services.approvals import collect_approval_items
from opi.services.catalog.base import ConfigLayer
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore
from opi.services.registry import SERVICES
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from fastapi import FastAPI

API_KEY = "test-api-key-12345"
SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"

#: Een domein dat het cluster niet zelf aanbiedt, dus een dat goedkeuring nodig heeft.
EIGEN_DOMEIN = "mijn-eigen-domein.nl"

PUBLISH_ON_WEB = ServiceType.PUBLISH_ON_WEB.value


# ---------------------------------------------------------------------------
# De schrijfweg: dezelfde aanvraag als de portal
# ---------------------------------------------------------------------------


def _make_manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _project(domains: dict[str, Any] | None = None) -> dict[str, Any]:
    """Een project met een deployment op het standaard clusteradres.

    ``domains`` vult het goedkeuringsblok voor, zodat een test een al goedgekeurd domein
    kan neerzetten. Het staat onder de dienst, waar de v2.5-migratie het naartoe bracht.
    """
    project: dict[str, Any] = {
        "name": "demo",
        "components": [{"name": "frontend", "services": [PUBLISH_ON_WEB]}],
        "deployments": [
            {
                "name": "prod",
                "cluster": "local",
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
    }
    if domains is not None:
        project["services"] = [{"reference": PUBLISH_ON_WEB, "config": {"domains": domains}}]
    return project


def _wire(pm, project_data: dict[str, Any]) -> AsyncMock:
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    save = AsyncMock()
    pm.save_and_commit_project = save
    return save


def _web_config(**overrides: Any) -> dict[str, Any]:
    return {"base-domain": EIGEN_DOMEIN, "domain-format": "subdomain", "subdomain": "app", **overrides}


def _allowed_domains(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    config = get_domains_config(project_data) or {}
    return [entry for entry in config.get("allowed-domains", []) if isinstance(entry, dict)]


class TestDeConfigApiMaaktDeAanvraag:
    """Een deployment-schrijfactie op publish-on-web maakt de ontbrekende aanvraag aan."""

    async def test_een_niet_goedgekeurd_domein_wordt_een_aanvraag(self) -> None:
        pm = _make_manager()
        project_data = _project()
        save = _wire(pm, project_data)

        result = await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        assert result["success"] is True
        assert [(e["domain"], e["status"]) for e in _allowed_domains(project_data)] == [(EIGEN_DOMEIN, "requested")]
        # In dezelfde commit als de config die hem veroorzaakte: een aanvraag die pas bij
        # de volgende schrijfactie wordt opgeslagen bestaat tot dan alleen in het geheugen.
        save.assert_awaited_once()

    async def test_de_aanvraag_krijgt_een_historieregel(self) -> None:
        """De beheerdersinterface toont de historie, dus die moet er vanaf regel een zijn."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        history = _allowed_domains(project_data)[0]["history"]
        assert [entry["status"] for entry in history] == ["requested"]
        assert history[0]["date"]

    async def test_een_goedgekeurd_domein_levert_geen_nieuwe_aanvraag(self) -> None:
        """Goedgekeurd blijft goedgekeurd -- een aanvraag ernaast zou het oordeel overrulen."""
        pm = _make_manager()
        goedgekeurd = {
            "allowed-domains": [
                {
                    "domain": EIGEN_DOMEIN,
                    "status": "approved",
                    "history": [{"date": "2026-01-01T00:00:00+00:00", "status": "approved", "by": "beheer"}],
                }
            ]
        }
        project_data = _project(domains=goedgekeurd)
        _wire(pm, project_data)

        result = await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        assert [(e["domain"], e["status"]) for e in _allowed_domains(project_data)] == [(EIGEN_DOMEIN, "approved")]
        assert len(_allowed_domains(project_data)[0]["history"]) == 1
        assert result["approvals"] == []

    async def test_tweemaal_schrijven_maakt_een_aanvraag(self) -> None:
        """Idempotent, want elke schrijfactie op de dienst loopt hier langs."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")
        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        assert len(_allowed_domains(project_data)) == 1

    async def test_het_standaard_clusteradres_vraagt_niets_aan(self) -> None:
        """Het domein van het cluster zelf is geen aanvraag; anders vraagt elk project er een aan."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        result = await pm.configure_service(
            PUBLISH_ON_WEB,
            "deployment",
            {"domain-format": "component-deployment-project"},
            deployment_name="prod",
        )

        assert get_domains_config(project_data) is None
        assert result["approvals"] == []


class TestDeAanvraagIsDezelfdeAanvraag:
    """Niet een tweede mechanisme: dezelfde opslag, dezelfde specs, dezelfde beheerdersinterface."""

    async def test_de_aanvraag_staat_onder_de_dienst_op_projectniveau(self) -> None:
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        entry = next(e for e in project_data["services"] if service_entry_name(e) == PUBLISH_ON_WEB)
        assert EIGEN_DOMEIN in str(service_entry_config(entry)["domains"])

    async def test_de_aanvraag_verschijnt_in_de_beheerdersinterface(self) -> None:
        """``collect_approval_items`` is wat /admin/approvals opsomt."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        items = [item for item in collect_approval_items(project_data) if item["type"] == "domain"]
        assert [(i["service"], i["name"], i["current_status"]) for i in items] == [
            (PUBLISH_ON_WEB, EIGEN_DOMEIN, "requested")
        ]

    async def test_de_beheerder_kan_het_oordeel_erop_vastleggen(self) -> None:
        """De aanvraag is bruikbaar aan de andere kant: ``record`` vindt hem terug."""
        from opi.services.approvals import apply_approval_verdicts

        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)
        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        items = collect_approval_items(project_data)
        for item in items:
            item["status"] = "approved"
        apply_approval_verdicts(project_data, items, "beheer@example.nl")

        entry = _allowed_domains(project_data)[0]
        assert entry["status"] == "approved"
        assert entry["history"][-1]["by"] == "beheer@example.nl"


class TestHetAntwoordMeldtDeWachtstand:
    """Een client hoort niet te ontdekken dat hij wacht doordat er geen ingress verschijnt."""

    async def test_het_antwoord_noemt_de_lopende_aanvraag_en_het_gevolg(self) -> None:
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        result = await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        notice = next(n for n in result["approvals"] if n["type"] == "domain")
        assert notice["service"] == PUBLISH_ON_WEB
        assert notice["status"] == "requested"
        assert notice["subject"] == EIGEN_DOMEIN
        # Het gevolg staat erbij: de deployment draait, maar op een ander adres.
        assert "clusteradres" in notice["text"]

    async def test_een_afgewezen_domein_meldt_dat_het_is_afgewezen(self) -> None:
        """Wachten en afgewezen zijn twee verschillende antwoorden."""
        pm = _make_manager()
        project_data = _project(
            domains={
                "allowed-domains": [
                    {
                        "domain": EIGEN_DOMEIN,
                        "status": "denied",
                        "history": [
                            {
                                "date": "2026-01-01T00:00:00+00:00",
                                "status": "denied",
                                "by": "beheer",
                                "message": "Niet van dit project",
                            }
                        ],
                    }
                ]
            }
        )
        _wire(pm, project_data)

        result = await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        notice = next(n for n in result["approvals"] if n["type"] == "domain")
        assert notice["status"] == "denied"
        assert notice["by"] == "beheer"
        assert notice["message"] == "Niet van dit project"

    async def test_een_schrijfactie_zonder_deployment_meldt_niets(self) -> None:
        """De goedkeuringen hangen aan een deployment, dus een componentschrijfactie zwijgt."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        result = await pm.configure_service(PUBLISH_ON_WEB, "component", {"tls": "standard"}, component_name="frontend")

        assert result["approvals"] == []


class TestDeDeploymentUpsertWijstNietMeerAf:
    """``POST .../deployments`` met een domein op aanvraag maakt de aanvraag in plaats van te falen."""

    async def test_een_domein_op_aanvraag_wordt_aangevraagd_in_plaats_van_geweigerd(self) -> None:
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        result = await pm.upsert_deployment(
            deployment_name="prod",
            components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v2")],
            base_domain=EIGEN_DOMEIN,
            domain_format="subdomain",
            subdomain="app",
        )

        assert result["success"] is True, result.get("error")
        assert [(e["domain"], e["status"]) for e in _allowed_domains(project_data)] == [(EIGEN_DOMEIN, "requested")]
        assert [n["status"] for n in result["approvals"]] == ["requested"]

    async def test_een_echte_configuratiefout_wordt_nog_steeds_geweigerd(self) -> None:
        """Alleen de "op aanvraag"-waarschuwing werd te hard opgevat; een fout blijft een fout.

        Een puntformaat op een domein dat geen punten ondersteunt levert een onbereikbare
        hostnaam op -- daar helpt geen goedkeuring tegen.
        """
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        result = await pm.upsert_deployment(
            deployment_name="prod",
            components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v2")],
            base_domain=EIGEN_DOMEIN,
            domain_format="component.subdomain",
            subdomain="app",
        )

        assert result["success"] is False
        assert result["error_type"] == "domain_validation"
        assert get_domains_config(project_data) is None


# ---------------------------------------------------------------------------
# De leesweg: de stand is later op te vragen
# ---------------------------------------------------------------------------


PROJECT_MET_AANVRAAG: dict[str, Any] = {
    "name": "test-project",
    "clusters": ["local"],
    "services": [
        {
            "reference": PUBLISH_ON_WEB,
            "config": {
                "domains": {
                    "allowed-domains": [
                        {
                            "domain": EIGEN_DOMEIN,
                            "status": "requested",
                            "history": [{"date": "2026-08-01T00:00:00+00:00", "status": "requested"}],
                        }
                    ]
                }
            },
        }
    ],
    "components": [
        {"name": "frontend", "type": "frontend", "services": [PUBLISH_ON_WEB], "ports": {"inbound": [3000]}}
    ],
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "namespace": "test-project",
            "repository": "main-repo",
            "components": [{"reference": "frontend", "image": "ghcr.io/org/frontend:1.0"}],
            "services": [{"reference": PUBLISH_ON_WEB, "config": _web_config()}],
        },
        {
            "name": "staging",
            "cluster": "local",
            "namespace": "test-project",
            "repository": "main-repo",
            "components": [{"reference": "frontend", "image": "ghcr.io/org/frontend:latest"}],
        },
    ],
}


@pytest.fixture
def mock_project_service() -> Any:
    mock_service = MagicMock(spec=GitProjectStore)
    test_project = ProjectSummary(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=PROJECT_MET_AANVRAAG,
    )

    def get_project(name: str) -> ProjectSummary | None:
        return test_project if name == "test-project" else None

    mock_service.get = get_project

    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.v2.router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def client(mock_settings: Any, mock_project_service: Any) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    argo = MagicMock()
    argo.auth_token = "fake-token"
    argo.get_application_status = AsyncMock(return_value=None)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_namespace_events = AsyncMock(return_value=[])
    with (
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_postfix", return_value=".local.test"),
        patch("opi.services.catalog.publish_on_web.urls.get_ingress_tls_enabled", return_value=False),
        patch("opi.api.v2.router.create_argo_connector", return_value=argo),
        patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl),
        patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="rig-test-project"),
    ):
        yield TestClient(app)


class TestDeStandIsLaterOpTeVragen:
    """Een aanvraag loopt dagen; het antwoord op de PUT is weg zodra de client hem las."""

    def test_de_deployment_meldt_waar_zijn_aanvraag_staat(self, client: TestClient) -> None:
        response = client.get("/api/v2/projects/test-project/deployments/production", headers={"X-API-Key": API_KEY})

        assert response.status_code == 200
        approvals = response.json()["approvals"]
        assert [(a["service"], a["type"], a["subject"], a["status"]) for a in approvals] == [
            (PUBLISH_ON_WEB, "domain", EIGEN_DOMEIN, "requested")
        ]
        assert "clusteradres" in approvals[0]["text"]

    def test_een_deployment_zonder_eigen_domein_meldt_niets(self, client: TestClient) -> None:
        response = client.get("/api/v2/projects/test-project/deployments/staging", headers={"X-API-Key": API_KEY})

        assert response.status_code == 200
        assert response.json()["approvals"] == []

    def test_de_lijst_meldt_het_ook(self, client: TestClient) -> None:
        """Anders zou een client per deployment moeten navragen wat de lijst al weet."""
        response = client.get("/api/v2/projects/test-project/deployments", headers={"X-API-Key": API_KEY})

        assert response.status_code == 200
        per_naam = {d["name"]: d["approvals"] for d in response.json()["deployments"]}
        assert [a["subject"] for a in per_naam["production"]] == [EIGEN_DOMEIN]
        assert per_naam["staging"] == []

    def test_het_veld_staat_in_de_openapi_uitvoer(self, client: TestClient) -> None:
        """Een terugkoppeling die niet beschreven is, bestaat niet voor een gegenereerde client."""
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

        assert "approvals" in schemas["DeploymentDetail"]["properties"]
        assert "approvals" in schemas["ConfigureServiceResult"]["properties"]
        notice = schemas["ApprovalNoticeResponse"]["properties"]
        assert set(notice) >= {"service", "type", "label", "subject", "status", "text"}
        assert notice["status"]["description"]


# ---------------------------------------------------------------------------
# domains op projectniveau blijft van het platform
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = {
        "task_id": SAMPLE_TASK_ID,
        "task_type": "configure_service",
        "status": "pending",
    }
    service.get_task.return_value = None
    return service


@pytest.fixture
def write_client(mock_settings: Any, mock_project_service: Any, mock_task_service: AsyncMock) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


class TestDomainsBlijftVanHetPlatform:
    """De status ontstaat als gevolg van een deployment-schrijfactie; de client schrijft hem nooit."""

    _DEPLOYMENT = f"/api/v2/projects/test-project/services/{PUBLISH_ON_WEB}/config/deployment/production"
    _COMPONENT = f"/api/v2/projects/test-project/services/{PUBLISH_ON_WEB}/config/component/frontend"

    _DOMAINS_BODY: ClassVar[dict[str, Any]] = {
        "domains": {"allowed-domains": [{"domain": EIGEN_DOMEIN, "status": "approved"}]}
    }

    def test_domains_meesturen_op_een_deployment_is_422(
        self, write_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """De weg waarlangs de aanvraag ontstaat is niet de weg om hem goed te keuren."""
        response = write_client.put(
            self._DEPLOYMENT, headers={"X-API-Key": API_KEY}, json={**_web_config(), **self._DOMAINS_BODY}
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_domains_meesturen_op_een_component_is_422(
        self, write_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        response = write_client.put(
            self._COMPONENT, headers={"X-API-Key": API_KEY}, json={"tls": "standard", **self._DOMAINS_BODY}
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_er_is_geen_schrijfroute_naar_het_projectniveau(self, write_client: TestClient) -> None:
        """Waar ``domains`` woont, schrijft de API helemaal niet.

        Twee sloten op dezelfde deur: er is vandaag geen project-laag route voor deze
        dienst, en het veld is als platform-eigendom gedeclareerd -- dus de dag dat die
        route er wel komt, weigert ``_refuse_platform_managed`` hem alsnog met 422.
        """
        paden = {
            route.path
            for route in write_client.app.routes  # type: ignore[attr-defined]
            if PUBLISH_ON_WEB in getattr(route, "path", "") and "config/project" in getattr(route, "path", "")
        }
        assert paden == set()

        service = SERVICES[ServiceType.PUBLISH_ON_WEB]
        assert service.platform_managed_fields(ConfigLayer.PROJECT) == frozenset({"domains"})

    def test_het_leesantwoord_geeft_domains_niet_terug(self, write_client: TestClient) -> None:
        """Wat je niet terugkrijgt, kun je ook niet per ongeluk terugsturen."""
        response = write_client.get(
            f"/api/v2/projects/test-project/services/{PUBLISH_ON_WEB}/config", headers={"X-API-Key": API_KEY}
        )

        assert response.status_code == 200
        configuraties = response.json()["configurations"]
        project_config = next(c["config"] for c in configuraties if c["target"] == "project")
        assert "domains" not in project_config
        # De deploymentconfig komt wel gewoon terug: die is van de client.
        deployment_config = next(c["config"] for c in configuraties if c["target"] == "deployment")
        assert deployment_config["base-domain"] == EIGEN_DOMEIN

    async def test_het_platform_schrijft_de_status_wel(self) -> None:
        """De keerzijde van de 422: als niemand hem mag schrijven, ontstaat er nooit een aanvraag."""
        pm = _make_manager()
        project_data = _project()
        _wire(pm, project_data)

        await pm.configure_service(PUBLISH_ON_WEB, "deployment", _web_config(), deployment_name="prod")

        assert _allowed_domains(project_data)[0]["status"] == "requested"
