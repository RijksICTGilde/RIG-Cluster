"""Tests for service-level change detection and cleanup.

Covers:
- cleanup_removed_services_from_yaml_change() orchestrator
- handle_service_removal() on each manager (database, minio, redis, keycloak)
- ServiceDefinition.cleanup_strategy values
- No false positives when services are unchanged
- Multiple services removed simultaneously
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.services import ServiceAdapter, ServiceType
from opi.services.services_enums import CleanupStrategy

# ---------------------------------------------------------------------------
# ServiceDefinition.cleanup_strategy tests
# ---------------------------------------------------------------------------


class TestCleanupStrategyDefinitions:
    """Verify cleanup_strategy is set correctly on all service definitions."""

    def test_deferred_services(self) -> None:
        deferred = [
            ServiceType.POSTGRESQL_DATABASE,
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
            ServiceType.MINIO_STORAGE,
            ServiceType.PERSISTENT_STORAGE,
        ]
        for svc_type in deferred:
            defn = ServiceAdapter.get_service_definition(svc_type)
            assert defn.cleanup_strategy is CleanupStrategy.DEFERRED, f"{svc_type.value} should be deferred"

    def test_immediate_services(self) -> None:
        immediate = [
            ServiceType.REDIS,
            ServiceType.NAMESPACE_REDIS,
            ServiceType.KEYCLOAK,
        ]
        for svc_type in immediate:
            defn = ServiceAdapter.get_service_definition(svc_type)
            assert defn.cleanup_strategy is CleanupStrategy.IMMEDIATE, f"{svc_type.value} should be immediate"

    def test_none_services(self) -> None:
        none_services = [
            ServiceType.PUBLISH_ON_WEB,
            ServiceType.TEMP_STORAGE,
            ServiceType.AUTHORIZATION_WALL,
        ]
        for svc_type in none_services:
            defn = ServiceAdapter.get_service_definition(svc_type)
            assert defn.cleanup_strategy is CleanupStrategy.NONE, f"{svc_type.value} should be none"

    def test_get_cleanable_service_types(self) -> None:
        cleanable = ServiceAdapter.get_cleanable_service_types()
        # Should include all deferred + immediate, not none
        cleanable_values = {s.value for s in cleanable}
        assert "postgresql-database" in cleanable_values
        assert "minio-storage" in cleanable_values
        assert "redis" in cleanable_values
        assert "keycloak" in cleanable_values
        # Should NOT include none-strategy services
        assert "publish-on-web" not in cleanable_values
        # persistent-storage is now deferred (PVC protection)
        assert "persistent-storage" in cleanable_values


# ---------------------------------------------------------------------------
# Helper: build project YAML with component → services mapping
# ---------------------------------------------------------------------------


def _make_project_yaml(
    project_name: str = "testproject",
    deployments: list[dict] | None = None,
    components: list[dict] | None = None,
) -> dict:
    """Build a minimal project YAML dict for testing."""
    yaml: dict = {"name": project_name}
    if deployments is not None:
        yaml["deployments"] = deployments
    if components is not None:
        yaml["components"] = components
    return yaml


def _make_deployment(
    name: str,
    cluster: str = "odcn",
    namespace: str = "test-ns",
    component_refs: list[str] | None = None,
) -> dict:
    dep: dict = {"name": name, "cluster": cluster, "namespace": namespace}
    if component_refs is not None:
        dep["components"] = [{"reference": ref} for ref in component_refs]
    return dep


def _make_component(name: str, services: list[str]) -> dict:
    return {"name": name, "services": services}


# ---------------------------------------------------------------------------
# handle_service_removal tests (individual managers)
# ---------------------------------------------------------------------------


class TestDatabaseHandleServiceRemoval:
    """DatabaseManager.handle_service_removal marks or deletes."""

    @pytest.fixture
    def db_manager(self) -> MagicMock:
        from opi.manager.database_manager import DatabaseManager

        mgr = MagicMock(spec=DatabaseManager)
        mgr.handle_service_removal = DatabaseManager.handle_service_removal.__get__(mgr)
        mgr.delete_resources_for_deployment = AsyncMock(
            return_value={"service": "database", "operations": [], "errors": [], "success": True}
        )
        return mgr

    @pytest.mark.asyncio
    async def test_marks_when_service_provided(self, db_manager: MagicMock) -> None:
        marked_svc = AsyncMock()
        result = await db_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=marked_svc,
        )
        assert marked_svc.mark_resource.call_count == 2
        resource_types = [c.kwargs["resource_type"] for c in marked_svc.mark_resource.call_args_list]
        assert "postgresql_database" in resource_types
        assert "postgresql_user" in resource_types
        assert any(op["type"] == "mark_for_deletion" for op in result["operations"])
        assert result["trigger"] == "service_removal"

    @pytest.mark.asyncio
    async def test_deletes_when_no_service(self, db_manager: MagicMock) -> None:
        result = await db_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=None,
        )
        db_manager.delete_resources_for_deployment.assert_awaited_once()
        assert result["trigger"] == "service_removal"


class TestMinioHandleServiceRemoval:
    """MinioManager.handle_service_removal marks or deletes."""

    @pytest.fixture
    def minio_manager(self) -> MagicMock:
        from opi.manager.minio_manager import MinioManager

        mgr = MagicMock(spec=MinioManager)
        mgr.handle_service_removal = MinioManager.handle_service_removal.__get__(mgr)
        mgr.delete_resources_for_deployment = AsyncMock(
            return_value={"service": "minio", "operations": [], "errors": [], "success": True}
        )
        return mgr

    @pytest.mark.asyncio
    async def test_marks_when_service_provided(self, minio_manager: MagicMock) -> None:
        marked_svc = AsyncMock()
        result = await minio_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=marked_svc,
        )
        assert marked_svc.mark_resource.call_count == 3
        resource_types = [c.kwargs["resource_type"] for c in marked_svc.mark_resource.call_args_list]
        assert "minio_bucket" in resource_types
        assert "minio_user" in resource_types
        assert "minio_policy" in resource_types
        assert result["trigger"] == "service_removal"

    @pytest.mark.asyncio
    async def test_deletes_when_no_service(self, minio_manager: MagicMock) -> None:
        result = await minio_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=None,
        )
        minio_manager.delete_resources_for_deployment.assert_awaited_once()
        assert result["trigger"] == "service_removal"


class TestRedisHandleServiceRemoval:
    """RedisManager.handle_service_removal always deletes immediately."""

    @pytest.fixture
    def redis_manager(self) -> MagicMock:
        from opi.manager.redis_manager import RedisManager

        mgr = MagicMock(spec=RedisManager)
        mgr.handle_service_removal = RedisManager.handle_service_removal.__get__(mgr)
        mgr.delete_resources_for_deployment = AsyncMock(
            return_value={"service": "redis", "operations": [], "errors": [], "success": True}
        )
        return mgr

    @pytest.mark.asyncio
    async def test_always_immediate(self, redis_manager: MagicMock) -> None:
        marked_svc = AsyncMock()
        result = await redis_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=marked_svc,
        )
        redis_manager.delete_resources_for_deployment.assert_awaited_once()
        marked_svc.mark_resource.assert_not_called()
        assert result["trigger"] == "service_removal"


class TestKeycloakHandleServiceRemoval:
    """KeycloakManager.handle_service_removal always deletes immediately."""

    @pytest.fixture
    def keycloak_manager(self) -> MagicMock:
        from opi.manager.keycloak_manager import KeycloakManager

        mgr = MagicMock(spec=KeycloakManager)
        mgr.handle_service_removal = KeycloakManager.handle_service_removal.__get__(mgr)
        mgr.delete_resources_for_deployment = AsyncMock(
            return_value={"service": "keycloak", "operations": [], "errors": [], "success": True}
        )
        return mgr

    @pytest.mark.asyncio
    async def test_always_immediate(self, keycloak_manager: MagicMock) -> None:
        marked_svc = AsyncMock()
        result = await keycloak_manager.handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data={"name": "dep", "cluster": "odcn"},
            project_data={},
            marked_for_deletion_service=marked_svc,
        )
        keycloak_manager.delete_resources_for_deployment.assert_awaited_once()
        marked_svc.mark_resource.assert_not_called()
        assert result["trigger"] == "service_removal"


# ---------------------------------------------------------------------------
# cleanup_removed_services_from_yaml_change orchestrator tests
# ---------------------------------------------------------------------------


class TestCleanupRemovedServicesOrchestrator:
    """Test the orchestrator in DeleteProjectManager."""

    @pytest.fixture
    def orchestrator(self) -> MagicMock:
        """Create a DeleteProjectManager with mocked managers."""
        from opi.manager.delete_project_manager import DeleteProjectManager

        pm = MagicMock()
        pm._project_file_handler = MagicMock()
        pm._minio_manager = MagicMock()
        pm._redis_manager = MagicMock()
        pm._keycloak_manager = MagicMock()

        # Make managers async
        pm._ensure_database_manager = AsyncMock()
        db_mgr = AsyncMock()
        db_mgr.handle_service_removal = AsyncMock(
            return_value={"service": "database", "operations": [], "errors": [], "success": True}
        )
        pm._ensure_database_manager.return_value = db_mgr

        pm._minio_manager.handle_service_removal = AsyncMock(
            return_value={"service": "minio", "operations": [], "errors": [], "success": True}
        )
        pm._redis_manager.handle_service_removal = AsyncMock(
            return_value={"service": "redis", "operations": [], "errors": [], "success": True}
        )
        pm._keycloak_manager.handle_service_removal = AsyncMock(
            return_value={"service": "keycloak", "operations": [], "errors": [], "success": True}
        )

        dpm = DeleteProjectManager(pm)
        return dpm

    def _setup_service_detection(
        self,
        orchestrator: MagicMock,
        previous_services: dict[str, list[str]],
        current_services: dict[str, list[str]],
    ) -> None:
        """Configure deployment_uses_service mock.

        Args:
            previous_services: {deployment_name: [service_type_values]} for previous YAML
            current_services: {deployment_name: [service_type_values]} for current YAML
        """
        previous_yaml_sentinel = object()
        current_yaml_sentinel = object()

        def uses_service(project_data, dep_name, svc_values):
            if project_data is previous_yaml_sentinel:
                return any(sv in previous_services.get(dep_name, []) for sv in svc_values)
            if project_data is current_yaml_sentinel:
                return any(sv in current_services.get(dep_name, []) for sv in svc_values)
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service
        # Store sentinels for assertion
        orchestrator._prev_sentinel = previous_yaml_sentinel
        orchestrator._curr_sentinel = current_yaml_sentinel

    @pytest.mark.asyncio
    async def test_no_surviving_deployments(self, orchestrator: MagicMock) -> None:
        previous = _make_project_yaml(deployments=[_make_deployment("dep-a")])
        current = _make_project_yaml(deployments=[_make_deployment("dep-b")])
        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["deployments_checked"] == 0
        assert result["services_removed"] == 0

    @pytest.mark.asyncio
    async def test_no_service_changes(self, orchestrator: MagicMock) -> None:
        """If services are unchanged, nothing is cleaned up."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        # deployment_uses_service returns same for both YAMLs
        orchestrator.project_manager._project_file_handler.deployment_uses_service.return_value = True

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["deployments_checked"] == 1
        assert result["services_removed"] == 0

    @pytest.mark.asyncio
    async def test_postgresql_removed_marks_deferred(self, orchestrator: MagicMock) -> None:
        """PostgreSQL removal triggers deferred marking via database manager."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            # Only postgresql-database was used before, not now
            if svc_values == [ServiceType.POSTGRESQL_DATABASE.value]:
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        marked_svc = AsyncMock()
        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
            marked_for_deletion_service=marked_svc,
        )
        assert result["services_removed"] == 1
        db_mgr = orchestrator.project_manager._ensure_database_manager.return_value
        db_mgr.handle_service_removal.assert_awaited_once()
        call_kwargs = db_mgr.handle_service_removal.call_args.kwargs
        assert call_kwargs["marked_for_deletion_service"] is marked_svc

    @pytest.mark.asyncio
    async def test_redis_removed_immediate_delete(self, orchestrator: MagicMock) -> None:
        """Redis removal triggers immediate delete via redis manager."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            if svc_values == [ServiceType.REDIS.value]:
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["services_removed"] == 1
        orchestrator.project_manager._redis_manager.handle_service_removal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keycloak_removed_immediate_delete(self, orchestrator: MagicMock) -> None:
        """Keycloak removal triggers immediate delete via keycloak manager."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            if svc_values == [ServiceType.KEYCLOAK.value]:
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["services_removed"] == 1
        orchestrator.project_manager._keycloak_manager.handle_service_removal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_minio_removed_marks_deferred(self, orchestrator: MagicMock) -> None:
        """MinIO removal triggers deferred marking via minio manager."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            if svc_values == [ServiceType.MINIO_STORAGE.value]:
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        marked_svc = AsyncMock()
        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
            marked_for_deletion_service=marked_svc,
        )
        assert result["services_removed"] == 1
        orchestrator.project_manager._minio_manager.handle_service_removal.assert_awaited_once()
        call_kwargs = orchestrator.project_manager._minio_manager.handle_service_removal.call_args.kwargs
        assert call_kwargs["marked_for_deletion_service"] is marked_svc

    @pytest.mark.asyncio
    async def test_multiple_services_removed_simultaneously(self, orchestrator: MagicMock) -> None:
        """Multiple services removed from a single deployment."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        removed_types = {
            ServiceType.POSTGRESQL_DATABASE.value,
            ServiceType.MINIO_STORAGE.value,
            ServiceType.REDIS.value,
        }

        def uses_service(project_data, dep_name, svc_values):
            if any(sv in removed_types for sv in svc_values):
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["services_removed"] == 3
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_service_added_not_treated_as_removal(self, orchestrator: MagicMock) -> None:
        """A service added (not in previous, in current) is not cleaned up."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            # Service only in current, not previous
            if svc_values == [ServiceType.REDIS.value]:
                return project_data is current
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["services_removed"] == 0

    @pytest.mark.asyncio
    async def test_manager_error_captured(self, orchestrator: MagicMock) -> None:
        """If a manager raises, the error is captured and reported."""
        dep = _make_deployment("dep-a")
        previous = _make_project_yaml(deployments=[dep])
        current = _make_project_yaml(deployments=[dep])

        def uses_service(project_data, dep_name, svc_values):
            if svc_values == [ServiceType.REDIS.value]:
                return project_data is previous
            return False

        orchestrator.project_manager._project_file_handler.deployment_uses_service.side_effect = uses_service
        orchestrator.project_manager._redis_manager.handle_service_removal = AsyncMock(
            side_effect=RuntimeError("connection failed")
        )

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        assert result["services_removed"] == 1
        assert result["success"] is False
        assert any("connection failed" in err for err in result["errors"])

    @pytest.mark.asyncio
    async def test_deleted_deployment_not_checked(self, orchestrator: MagicMock) -> None:
        """Deployments only in previous YAML (deleted) are not checked here."""
        previous = _make_project_yaml(deployments=[_make_deployment("dep-a"), _make_deployment("dep-deleted")])
        current = _make_project_yaml(deployments=[_make_deployment("dep-a")])

        orchestrator.project_manager._project_file_handler.deployment_uses_service.return_value = False

        result = await orchestrator.cleanup_removed_services_from_yaml_change(
            project_name="proj",
            previous_yaml=previous,
            current_yaml=current,
        )
        # Only dep-a is surviving, dep-deleted is not checked
        assert result["deployments_checked"] == 1
