"""Een projectnaam mag niet op een platformnamespace uitkomen (RC-365).

De namespace van een project is de clusterprefix plus de projectnaam, dus een naam als
``operations`` of ``system`` landt in de namespace van het platform zelf. Daar schrijft
OPI de projectsleutel over de platformsleutel heen, en een projectverwijdering neemt de
namespace mee. Drie lagen worden hier getoetst: de afleiding uit CLUSTER_CONFIG, de
weigering op elke aanmaakweg, en de twee guards die het schrijven en verwijderen
tegenhouden als de naam er toch doorheen komt.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError
from opi.core.cluster_config import (
    CLUSTER_CONFIG,
    RESERVED_NAMESPACE_PREFIXES,
    get_platform_namespaces,
    is_platform_namespace,
    is_reserved_project_name,
)
from opi.core.project_schema import ProjectIntegrityError
from opi.handlers.sops import SopsHandler
from opi.manager.project_validation import validate_project_structure

PLATFORM_NAMESPACE = "rig-prd-operations"

# De namespacevelden van een cluster, hier met de hand genoemd in plaats van uit
# cluster_config geimporteerd: anders zou het weghalen van een veld daar deze toets
# meeveranderen in plaats van rood maken.
NAMESPACE_VELDEN = (
    "namespace",
    "argo_namespace",
    "backup_namespace",
    "mail_relay_namespace",
    "database_operator_namespace",
)


# ---------------------------------------------------------------------------
# Punt 1: de afleiding uit CLUSTER_CONFIG
# ---------------------------------------------------------------------------


class TestGereserveerdeNamen:
    @pytest.mark.parametrize("name", ["operations", "backup", "ron", "system", "backup-destination"])
    def test_een_platformnaam_is_gereserveerd(self, name: str) -> None:
        assert is_reserved_project_name(name) is True

    @pytest.mark.parametrize("name", ["algor-odc", "demo", "operations-portaal", "backupje", ""])
    def test_een_gewone_naam_is_niet_gereserveerd(self, name: str) -> None:
        assert is_reserved_project_name(name) is False

    def test_de_vaste_systeemvoorvoegsels_zijn_platformnamespaces(self) -> None:
        for prefix in RESERVED_NAMESPACE_PREFIXES:
            assert is_platform_namespace(f"{prefix}iets") is True

    def test_een_projectnamespace_is_geen_platformnamespace(self) -> None:
        assert is_platform_namespace("rig-prd-algor-odc") is False


class TestDeAfleidingBlijftCompleet:
    """Komt er in CLUSTER_CONFIG een platformnamespace bij, dan gaat dit rood."""

    def test_elke_namespace_uit_de_clusterconfig_wordt_gezien(self) -> None:
        bekend = get_platform_namespaces()
        for cluster_name, cluster_config in CLUSTER_CONFIG.items():
            for key in NAMESPACE_VELDEN:
                value = cluster_config.get(key)
                if value:
                    assert value in bekend, f"{cluster_name}.{key} = {value} wordt niet als platform gezien"
            for value in cluster_config.get("service_namespaces", []):
                assert value in bekend, f"{cluster_name}.service_namespaces bevat {value}, niet als platform gezien"

    def test_er_is_geen_namespaceveld_dat_de_afleiding_mist(self) -> None:
        """Een nieuw ``*_namespace``-veld moet in NAMESPACE_VELDEN hierboven komen."""
        toegestaan = {*NAMESPACE_VELDEN, "service_namespaces", "namespace_prefix"}
        for cluster_name, cluster_config in CLUSTER_CONFIG.items():
            onbekend = {key for key in cluster_config if "namespace" in key} - toegestaan
            assert not onbekend, f"{cluster_name} heeft namespacevelden die de afleiding niet leest: {onbekend}"


# ---------------------------------------------------------------------------
# Punt 2: de weigering op elke aanmaakweg
# ---------------------------------------------------------------------------


class TestWeigeringBijDeStore:
    """``validate_project_structure`` draait de regel alleen zonder vorige versie."""

    async def test_een_create_met_een_gereserveerde_naam_wordt_geweigerd(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="niet beschikbaar"):
            await validate_project_structure({"name": "operations"}, previous=None)

    async def test_een_bestaand_project_mag_opgeslagen_blijven_worden(self) -> None:
        project = {"name": "operations"}
        await validate_project_structure(project, previous=project)

    async def test_een_gewone_naam_gaat_door(self) -> None:
        await validate_project_structure({"name": "algor-odc"}, previous=None)


class TestWeigeringBijDeTaak:
    """``handle_create_project`` faalt de taak in plaats van hem te laten draaien."""

    @staticmethod
    def _progress() -> MagicMock:
        progress = MagicMock()
        progress.add_task.side_effect = lambda name: f"task-{name}"
        return progress

    async def test_een_nieuw_project_met_een_gereserveerde_naam_faalt(self) -> None:
        from opi.core.task_handlers_project import handle_create_project

        progress = self._progress()
        result = await handle_create_project({"project_name": "operations", "is_new_project": True}, progress)

        assert result["status"] == "failed"
        assert result["error_type"] == "invalid_project_name"
        assert "niet beschikbaar" in result["error"]
        progress.fail_project.assert_called_once()

    async def test_een_bestaand_project_wordt_hier_niet_geweigerd(self) -> None:
        """Zonder ``is_new_project`` komt de taak voorbij de naamtoets.

        Hij struikelt daarna op de ontbrekende YAML, en dat is precies het bewijs dat de
        naamregel hem niet heeft tegengehouden.
        """
        from opi.core.task_handlers_project import handle_create_project

        progress = self._progress()
        with patch("opi.services.project_store.get_project_store", side_effect=RuntimeError("doorgelaten")):
            result = await handle_create_project({"project_name": "operations"}, progress)

        assert result.get("error_type") != "invalid_project_name"


class TestWeigeringBijDeApi:
    async def test_self_service_geeft_400(self) -> None:
        from fastapi import HTTPException
        from opi.api.router import SelfServiceProjectRequest, create_self_service_project

        request = SelfServiceProjectRequest(project_name="operations", display_name="Operations", cluster="local")
        with pytest.raises(HTTPException) as excinfo:
            await create_self_service_project(MagicMock(), request)

        assert excinfo.value.status_code == 400
        assert "niet beschikbaar" in str(excinfo.value.detail)


class TestWeigeringBijEenGitCommit:
    """Een projectbestand dat voor het eerst in zad-projects opduikt, is een create."""

    @staticmethod
    def _manager_met(previous_yaml: dict | None, name: str):
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

            pm = ProjectManager()

        pm._project_file_handler = MagicMock()
        pm._project_file_handler.analyze_project_changes = AsyncMock(
            return_value={
                "current_yaml": {"name": name},
                "previous_yaml": previous_yaml,
                "changes": {"added": {}, "changed": {}, "deleted": {}},
            }
        )
        pm._project_file_handler.was_migrated = False
        pm._analyze_deployment_changes = MagicMock(return_value={"added": {}, "changed": {}, "deleted": {}})
        pm.get_git_connector_for_project_files = AsyncMock(return_value=MagicMock())
        pm.process_project = AsyncMock(return_value=True)
        pm.close = AsyncMock()
        return pm

    async def test_een_nieuw_bestand_met_een_gereserveerde_naam_wordt_geweigerd(self) -> None:
        pm = self._manager_met(previous_yaml=None, name="operations")

        with patch("opi.manager.project_manager.validate_project_schema"):
            assert await pm.process_project_from_git("projects/operations.yaml") is False

        assert "niet beschikbaar" in str(pm.get_processing_error())
        pm.process_project.assert_not_awaited()

    async def test_een_bestaand_bestand_wordt_niet_geweigerd(self) -> None:
        """Verwerken gaat verder tot ``process_project``, en dat is de hele claim."""
        pm = self._manager_met(previous_yaml={"name": "operations"}, name="operations")

        with patch("opi.manager.project_manager.validate_project_schema"):
            await pm.process_project_from_git("projects/operations.yaml")

        pm.process_project.assert_awaited()


# ---------------------------------------------------------------------------
# Punt 3: de tweede laag, bij het schrijven en het verwijderen
# ---------------------------------------------------------------------------


class TestDeSleutelLandtNooitInEenPlatformnamespace:
    async def test_schrijven_in_een_platformnamespace_gooit(self) -> None:
        kubectl = MagicMock()
        kubectl.apply_manifest = AsyncMock()
        handler = SopsHandler(kubectl_connector=kubectl)

        with pytest.raises(ValueError, match="platform zelf"):
            await handler.store_project_sops_key_in_namespace(PLATFORM_NAMESPACE, "AGE-SECRET-KEY-1x", "age1x")

        kubectl.apply_manifest.assert_not_awaited()

    async def test_schrijven_in_een_projectnamespace_gaat_door(self) -> None:
        kubectl = MagicMock()
        kubectl.apply_manifest = AsyncMock()
        handler = SopsHandler(kubectl_connector=kubectl)

        assert await handler.store_project_sops_key_in_namespace("rig-prd-demo", "AGE-SECRET-KEY-1x", "age1x") is True
        kubectl.apply_manifest.assert_awaited_once()


class TestEenPlatformnamespaceWordtNooitVerwijderd:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        KubectlConnector._instance = None
        KubectlConnector._initialized = False
        yield
        KubectlConnector._instance = None
        KubectlConnector._initialized = False

    @pytest.fixture
    def connector(self) -> KubectlConnector:
        with patch("opi.connectors.kubectl.asyncio.create_task", new=MagicMock()):
            return KubectlConnector()

    async def test_verwijderen_van_een_platformnamespace_gooit(self, connector: KubectlConnector) -> None:
        with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as run:
            with pytest.raises(KubectlExecutionError, match="platform zelf"):
                await connector.delete_namespace(PLATFORM_NAMESPACE)

            run.assert_not_awaited()

    async def test_verwijderen_van_een_projectnamespace_gaat_door(self, connector: KubectlConnector) -> None:
        with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as run:
            run.return_value = ('namespace "rig-prd-demo" deleted', "", 0)

            assert await connector.delete_namespace("rig-prd-demo") is True
            run.assert_awaited_once()
