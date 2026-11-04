"""De vier handlers geven `deployment_names` uit de payload door aan de verwerking (RC-418).

De scope wordt opgelost waar de taak wordt gemaakt (``affected_deployments`` in de
router) en op precies een plek verwerkt: ``process_project_from_git``. Zonder deze
doorgifte zou de router de scope wel berekenen, maar de verwerking zelf toch het hele
project nemen -- de versmalling zou dan niets opleveren.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PM_PATH = "opi.manager.project_manager.ProjectManager"


def _progress():
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name: f"subtask-{name}"
    return progress


class TestUpdateComponentForwardsScope:
    @pytest.mark.asyncio
    async def test_de_deployment_names_uit_de_payload_bereiken_de_verwerking(self) -> None:
        from opi.core.task_handlers_components import handle_update_component

        pm = AsyncMock()
        pm.update_component = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            await handle_update_component(
                {"project_name": "test-project", "name": "web", "deployment_names": ["productie"]}, _progress()
            )

        assert pm.process_project_from_git.call_args.kwargs["deployment_names"] == ["productie"]

    @pytest.mark.asyncio
    async def test_zonder_lijst_blijft_de_verwerking_projectbreed(self) -> None:
        from opi.core.task_handlers_components import handle_update_component

        pm = AsyncMock()
        pm.update_component = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            await handle_update_component({"project_name": "test-project", "name": "web"}, _progress())

        assert pm.process_project_from_git.call_args.kwargs["deployment_names"] is None


class TestAddServiceForwardsScope:
    @pytest.mark.asyncio
    async def test_de_deployment_names_uit_de_payload_bereiken_de_verwerking(self) -> None:
        from opi.core.task_handlers_components import handle_add_service

        pm = AsyncMock()
        pm.add_service = AsyncMock(return_value={"success": True, "services_added": ["keycloak"]})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            await handle_add_service(
                {"project_name": "test-project", "service": "keycloak", "deployment_names": ["acceptatie"]},
                _progress(),
            )

        assert pm.process_project_from_git.call_args.kwargs["deployment_names"] == ["acceptatie"]


class TestConfigureServiceForwardsScope:
    @pytest.mark.asyncio
    async def test_de_deployment_names_uit_de_payload_bereiken_de_verwerking(self) -> None:
        from opi.core.task_handlers_components import handle_configure_service

        pm = AsyncMock()
        pm.configure_service = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            await handle_configure_service(
                {
                    "project_name": "test-project",
                    "service": "keycloak",
                    "target": "project",
                    "operation": "upsert",
                    "config": {"template": "x"},
                    "deployment_names": ["productie", "acceptatie"],
                },
                _progress(),
            )

        assert pm.process_project_from_git.call_args.kwargs["deployment_names"] == ["productie", "acceptatie"]


class TestConfigureServiceValuesForwardsScope:
    @pytest.mark.asyncio
    async def test_de_deployment_names_uit_de_payload_bereiken_de_verwerking(self) -> None:
        from opi.core.task_handlers_components import handle_configure_service_values

        pm = AsyncMock()
        pm.set_component_values = AsyncMock(return_value={"success": True, "changed": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            await handle_configure_service_values(
                {
                    "project_name": "test-project",
                    "service": "keycloak",
                    "target": "component",
                    "operation": "set",
                    "component": "web",
                    "values": {"x": 1},
                    "deployment_names": ["productie"],
                },
                _progress(),
            )

        assert pm.process_project_from_git.call_args.kwargs["deployment_names"] == ["productie"]
