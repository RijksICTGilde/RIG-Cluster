"""POST /services binds a component even when the service is already selected.

The reported failure is the worst kind there is: a write that answers ``success`` and
``components_updated: [api]`` while the project file never changed. It came from
gating the SAVE on ``services_added`` alone --

    {"service": "health-check", "components": ["web"]}   -> added, web bound      OK
    {"service": "health-check", "components": ["api"]}   -> skipped, api NOT bound

-- which is exactly the ordinary order of work: configure a service, then bind it to
the next component. The mutation happened in memory and was thrown away, and the
response reported it as done, so no caller could tell.

Two rules, measured here:

1. **A component binding is a change.** ``services_added`` empty does not mean nothing
   happened; the save gate asks whether the project data changed, not which half of it.
2. **The rollout follows the same question.** A newly bound component needs its
   manifests just as much as a newly selected service does, so the processing gate in
   both the async task handler and the sync route reads the same two lists.

``components_updated`` itself was already honest -- it only lists components whose
services list actually grew -- and the third test pins that, because the report read it
as an echo of the request and it must never become one.

Git is mocked at ``save_and_commit_project``: the subject is what gets persisted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SERVICE = "health-check"


def _manager():
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


def _project(*, services: list[Any], components: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "demo",
        "services": services,
        "components": components,
        "deployments": [],
    }


def _wire(project_data: dict[str, Any]):
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.get_name = AsyncMock(return_value="demo")
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


def _component_services(project_data: dict[str, Any], name: str) -> list[Any]:
    for comp in project_data["components"]:
        if comp["name"] == name:
            return comp.get("services", [])
    raise AssertionError(f"no component {name}")


class TestSecondCallBinds:
    """The service is already on the project; a second call binds another component."""

    @pytest.mark.asyncio
    async def test_binding_an_already_selected_service_is_persisted(self):
        project_data = _project(
            services=[SERVICE],
            components=[{"name": "web", "services": [SERVICE]}, {"name": "api", "services": []}],
        )
        manager, save = _wire(project_data)

        result = await manager.add_service(service_name=SERVICE, component_names=["api"])

        assert result["success"] is True
        assert result["services_added"] == []
        assert result["components_updated"] == ["api"]
        assert _component_services(project_data, "api") == [SERVICE]
        assert save.await_count == 1, "the component binding was reported but never saved"

    @pytest.mark.asyncio
    async def test_nothing_to_do_still_saves_nothing(self):
        project_data = _project(
            services=[SERVICE],
            components=[{"name": "web", "services": [SERVICE]}],
        )
        manager, save = _wire(project_data)

        result = await manager.add_service(service_name=SERVICE, component_names=["web"])

        assert result["services_added"] == []
        assert result["components_updated"] == []
        save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_components_updated_names_what_changed_not_the_request(self):
        project_data = _project(
            services=[SERVICE],
            components=[{"name": "web", "services": [SERVICE]}, {"name": "api", "services": []}],
        )
        manager, _ = _wire(project_data)

        result = await manager.add_service(service_name=SERVICE, component_names=["web", "api"])

        assert result["components_updated"] == ["api"]


class TestRolloutFollowsTheBinding:
    """A newly bound component needs its manifests, so the processing gate reads both lists."""

    @pytest.mark.asyncio
    async def test_task_handler_processes_after_a_binding_only(self):
        from opi.core.task_handlers_components import handle_add_service

        pm = MagicMock()
        pm.add_service = AsyncMock(return_value={"success": True, "services_added": [], "components_updated": ["api"]})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = MagicMock()
        progress.add_task.side_effect = lambda name: f"task-{name}"

        with patch("opi.manager.project_manager.ProjectManager", return_value=pm):
            result = await handle_add_service(
                {"project_name": "demo", "service": SERVICE, "components": ["api"]},
                progress,
            )

        pm.process_project_from_git.assert_awaited_once()
        assert result["processing"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_task_handler_skips_when_truly_nothing_changed(self):
        from opi.core.task_handlers_components import handle_add_service

        pm = MagicMock()
        pm.add_service = AsyncMock(return_value={"success": True, "services_added": [], "components_updated": []})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = MagicMock()
        progress.add_task.side_effect = lambda name: f"task-{name}"

        with patch("opi.manager.project_manager.ProjectManager", return_value=pm):
            result = await handle_add_service({"project_name": "demo", "service": SERVICE}, progress)

        pm.process_project_from_git.assert_not_awaited()
        assert result["processing"]["status"] == "skipped"
