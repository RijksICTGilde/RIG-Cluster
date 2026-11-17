"""A deployment's own storage size, over the API.

This layer had no route: the v2 router generated config routes for project, component and
deployment and left the deployment-component layer out on purpose, because no service accepted
config there. The two storage services do now, and are the first to ask for the route.

What this file pins:

* the route exists, and only for the services that ask for it -- ``user-env-vars`` may never
  get one, because that override goes through the step-up window only (RC-388);
* the body is the size per volume and nothing else: ``generation``, ``revisions`` and the
  approval block live in those same records and stay as they are;
* a volume the component does not mount is a 422, not a setting that silently does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.services.catalog.base import ConfigLayer, config_endpoint_path
from opi.services.catalog.shared.storage_size import effective_mount_size
from opi.services.registry import SERVICES
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from fastapi import FastAPI

API_KEY = "test-api-key-12345"
SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"

PERSISTENT = ServiceType.PERSISTENT_STORAGE.value
OVERRIDE_PATH = f"/api/v2/projects/test-project/services/{PERSISTENT}/config/deployment/meten/component/loep"


def _project() -> dict[str, Any]:
    return {
        "name": "test-project",
        "services": ["persistent-storage"],
        "components": [
            {
                "name": "loep",
                "type": "single",
                "services": [
                    {
                        "reference": "persistent-storage",
                        "config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}],
                    }
                ],
            }
        ],
        "deployments": [{"name": "meten", "cluster": "local", "components": [{"reference": "loep"}]}],
    }


@pytest.fixture
def project_data() -> dict[str, Any]:
    return _project()


@pytest.fixture
def task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = {
        "task_id": SAMPLE_TASK_ID,
        "task_type": "configure_service",
        "status": "pending",
    }
    service.get_task.return_value = None
    return service


@pytest.fixture
def client(mock_settings: Any, mock_project_service: Any, task_service: AsyncMock, project_data: dict[str, Any]):
    from opi.server import create_app

    store = MagicMock()
    store.get.return_value = MagicMock(name="test-project", data=project_data, api_key=API_KEY, users=[])
    app: FastAPI = create_app()
    app.state.task_service = task_service
    with patch("opi.api.v2.router.get_project_store", return_value=store):
        yield TestClient(app)


def _body(size: str | None, reference: str = "data") -> list[dict[str, Any]]:
    """The body IS the list: the model is a ``RootModel[list[...]]``."""
    return [{"reference": reference, "config": {} if size is None else {"size": size}}]


class TestTheRouteExists:
    def test_the_storage_services_have_a_route_on_this_layer(self, client: TestClient) -> None:
        paths = {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]
        for service in (ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE):
            # Through ``config_endpoint_path``, so the route that gets registered and the path
            # an error message sends a client to are the same string.
            assert config_endpoint_path(ConfigLayer.DEPLOYMENT_COMPONENT, service.value) in paths

    def test_user_env_vars_has_no_route_here(self, client: TestClient) -> None:
        """The values can hold secrets; only the step-up window writes them."""
        spec = client.get("/openapi.json").json()
        assert not [
            candidate
            for candidate in spec["paths"]
            if ServiceType.USER_ENV_VARS.value in candidate
            and "/config/deployment/" in candidate
            and "/component/" in candidate
        ]

    def test_no_other_service_asks_for_it_today(self) -> None:
        """Opt-in, not automatic: a service with config on this layer does not get an
        endpoint by accident."""
        asking = {
            st.value for st, svc in SERVICES.items() if ConfigLayer.DEPLOYMENT_COMPONENT in svc.config_api_write_layers
        }
        assert asking == {PERSISTENT, ServiceType.TEMP_STORAGE.value}


class TestTheWritePath:
    def test_an_override_within_the_free_range_is_accepted(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.put(OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json=_body("2Gi"))

        assert response.status_code == 202
        payload = task_service.create_task.await_args.kwargs["payload"]
        assert payload["target"] == ConfigLayer.DEPLOYMENT_COMPONENT.value
        assert payload["config"] == [{"reference": "data", "config": {"size": "2Gi"}}]
        assert payload["deployment"] == "meten"
        assert payload["component"] == "loep"

    def test_a_size_outside_the_range_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.put(OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json=_body("60Gi"))

        assert response.status_code == 422
        task_service.create_task.assert_not_called()

    def test_an_unknown_volume_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.put(OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json=_body("1Gi", reference="weg"))

        assert response.status_code == 422
        assert "weg" in response.json()["detail"]
        task_service.create_task.assert_not_called()

    def test_sending_the_approval_block_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        """The path that creates the request is not the path that approves it."""
        response = client.put(
            OVERRIDE_PATH,
            headers={"X-API-Key": API_KEY},
            json=[{"reference": "data", "config": {"size": "2Gi", "size-approval": {"status": "approved"}}}],
        )

        assert response.status_code == 422
        task_service.create_task.assert_not_called()

    def test_sending_the_generation_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.put(
            OVERRIDE_PATH,
            headers={"X-API-Key": API_KEY},
            json=[{"reference": "data", "config": {"size": "2Gi", "generation": 7}}],
        )

        assert response.status_code == 422
        task_service.create_task.assert_not_called()

    def test_an_unknown_deployment_is_404(self, client: TestClient) -> None:
        candidate = OVERRIDE_PATH.replace("/deployment/meten/", "/deployment/bestaat-niet/")
        response = client.put(candidate, headers={"X-API-Key": API_KEY}, json=_body("1Gi"))

        assert response.status_code == 404


class TestThePatch:
    """The PATCH on this layer: one volume at a time, without resending the others.

    It is generated from the body model (``StorageSizeOverrides``), and the handler used to
    read the stored model instead, so every call ended on "has no patchable config at the
    deployment-component layer".
    """

    def test_one_volume_is_patched_without_the_others(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.patch(OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json={"add": _body("2Gi"), "remove": []})

        assert response.status_code == 202
        payload = task_service.create_task.await_args.kwargs["payload"]
        assert payload["operation"] == "patch"
        assert payload["add"] == [{"reference": "data", "config": {"size": "2Gi"}}]

    def test_a_size_outside_the_range_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.patch(
            OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json={"add": _body("60Gi"), "remove": []}
        )

        assert response.status_code == 422
        task_service.create_task.assert_not_called()

    def test_an_unknown_volume_is_422(self, client: TestClient, task_service: AsyncMock) -> None:
        """Same rule as the PUT: the entries a patch adds land in that same list."""
        response = client.patch(
            OVERRIDE_PATH, headers={"X-API-Key": API_KEY}, json={"add": _body("1Gi", reference="weg"), "remove": []}
        )

        assert response.status_code == 422
        assert "weg" in response.json()["detail"]
        task_service.create_task.assert_not_called()

    def test_the_generation_cannot_ride_along(self, client: TestClient, task_service: AsyncMock) -> None:
        response = client.patch(
            OVERRIDE_PATH,
            headers={"X-API-Key": API_KEY},
            json={"add": [{"reference": "data", "config": {"size": "2Gi", "generation": 7}}], "remove": []},
        )

        assert response.status_code == 422
        task_service.create_task.assert_not_called()

    async def test_the_patch_leaves_the_platform_fields_of_the_volume_alone(self) -> None:
        """The generation and the verdict live in the record the patch rewrites."""
        data = _project()
        data["deployments"][0]["components"][0]["services"] = {
            PERSISTENT: [
                {"reference": "data", "config": {"generation": 3, "size-approval": {"status": "approved"}}},
            ]
        }

        counts = ServiceAdapter.patch_service_config_list(
            data,
            PERSISTENT,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            add=[{"reference": "data", "config": {"size": "2Gi"}}],
            remove=[],
            component_name="loep",
            deployment_name="meten",
        )

        assert counts == {"added": 0, "updated": 1, "removed": 0}
        config = data["deployments"][0]["components"][0]["services"][PERSISTENT][0]["config"]
        assert config["size"] == "2Gi"
        assert config["generation"] == 3
        assert config["size-approval"] == {"status": "approved"}

    async def test_a_remove_withdraws_the_override_and_keeps_the_generation(self) -> None:
        """The per-item merge cannot express a removal, so the patch drops the record itself."""
        data = _project()
        data["deployments"][0]["components"][0]["services"] = {
            PERSISTENT: [
                {"reference": "data", "config": {"size": "5Gi", "generation": 3}},
            ]
        }

        counts = ServiceAdapter.patch_service_config_list(
            data,
            PERSISTENT,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            add=[],
            remove=["data"],
            component_name="loep",
            deployment_name="meten",
        )

        assert counts == {"added": 0, "updated": 0, "removed": 1}
        records = data["deployments"][0]["components"][0]["services"][PERSISTENT]
        assert records == [{"reference": "data", "config": {"generation": 3}}]
        assert effective_mount_size(data, ServiceType.PERSISTENT_STORAGE, ("meten", "loep", "data")) == "1Gi"

    async def test_a_remove_of_a_record_without_platform_state_drops_it(self) -> None:
        data = _project()
        data["deployments"][0]["components"][0]["services"] = {
            PERSISTENT: [{"reference": "data", "config": {"size": "5Gi"}}]
        }

        counts = ServiceAdapter.patch_service_config_list(
            data,
            PERSISTENT,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            add=[],
            remove=["data"],
            component_name="loep",
            deployment_name="meten",
        )

        assert counts == {"added": 0, "updated": 0, "removed": 1}
        assert "services" not in data["deployments"][0]["components"][0] or not data["deployments"][0]["components"][0][
            "services"
        ].get(PERSISTENT)

    async def test_a_remove_of_a_volume_without_an_override_counts_nothing(self) -> None:
        data = _project()

        counts = ServiceAdapter.patch_service_config_list(
            data,
            PERSISTENT,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            add=[],
            remove=["data"],
            component_name="loep",
            deployment_name="meten",
        )

        assert counts == {"added": 0, "updated": 0, "removed": 0}


class TestTheWayIntoTheFile:
    """What the task does with the body: the same logic the form runs."""

    async def test_the_override_lands_on_the_volume_and_requests_its_decision(self) -> None:
        with (
            patch("opi.manager.project_manager.KubectlConnector"),
            patch("opi.services.project_key_secret.ProjectKeySecretHandler"),
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

            manager = ProjectManager()
        data = _project()
        manager.get_contents = AsyncMock(return_value=data)
        manager.get_name = AsyncMock(return_value="test-project")
        manager.save_and_commit_project = AsyncMock(return_value=MagicMock(committed=True))

        result = await manager.configure_service(
            PERSISTENT,
            ConfigLayer.DEPLOYMENT_COMPONENT.value,
            [{"reference": "data", "config": {"size": "10Gi"}}],
            deployment_name="meten",
            component_name="loep",
        )

        assert result["success"] is True
        records = data["deployments"][0]["components"][0]["services"][PERSISTENT]
        assert records[0]["reference"] == "data"
        assert records[0]["config"]["size"] == "10Gi"
        # The request is in the same commit as the override that caused it.
        assert records[0]["config"]["size-approval"]["status"] == "requested"
        assert effective_mount_size(data, ServiceType.PERSISTENT_STORAGE, ("meten", "loep", "data")) == "2Gi"
