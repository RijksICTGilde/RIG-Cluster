"""Characterization tests for ``ProjectManager.add_component_to_deployment``.

The safety net for the move in RC-443: the method had no direct test, only mocked handler
tests. These tests record the behavior as it IS, down to the component reference appended
to the deployment, the commit message, and each of the three refusals.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from opi.manager.project_manager import ProjectManager


def _manager() -> ProjectManager:
    pm = ProjectManager.__new__(ProjectManager)
    pm._project_file_handler = MagicMock()
    pm._ProjectManager__has_contents = True
    pm.get_name = AsyncMock(return_value="demo")
    pm.save_and_commit_project = AsyncMock()
    return pm


def _project() -> dict[str, Any]:
    return {
        "name": "demo",
        "components": [{"name": "web"}, {"name": "worker"}],
        "deployments": [
            {"name": "prod", "components": [{"reference": "web", "image": "ghcr.io/org/web:v1"}]},
            {"name": "acc", "components": []},
        ],
    }


class TestAddComponentToDeployment:
    async def test_the_reference_is_appended_and_committed(self) -> None:
        pm = _manager()
        project_data = _project()
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.add_component_to_deployment("prod", "worker", "ghcr.io/org/worker:v2")

        assert result == {
            "success": True,
            "component_reference": {"reference": "worker", "image": "ghcr.io/org/worker:v2"},
        }
        assert project_data["deployments"][0]["components"] == [
            {"reference": "web", "image": "ghcr.io/org/web:v1"},
            {"reference": "worker", "image": "ghcr.io/org/worker:v2"},
        ]
        saved, message = pm.save_and_commit_project.await_args.args
        assert saved is project_data
        assert message == "Add component 'worker' to deployment 'prod' in project 'demo'"

    async def test_a_deployment_without_a_components_key_gets_one(self) -> None:
        pm = _manager()
        project_data = _project()
        del project_data["deployments"][1]["components"]
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.add_component_to_deployment("acc", "web", "ghcr.io/org/web:v1")

        assert result["success"] is True
        assert project_data["deployments"][1]["components"] == [{"reference": "web", "image": "ghcr.io/org/web:v1"}]

    async def test_an_uppercase_image_is_lowercased_and_reported_as_a_warning(self) -> None:
        pm = _manager()
        project_data = _project()
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.add_component_to_deployment("acc", "worker", "ghcr.io/Org/Worker:V2")

        assert result["component_reference"]["image"] == "ghcr.io/org/worker:v2"
        assert result["warnings"] == [
            "Image was normalized to lowercase: 'ghcr.io/Org/Worker:V2' -> 'ghcr.io/org/worker:v2'"
        ]

    async def test_a_lowercase_image_reports_no_warnings_key(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(return_value=_project())

        result = await pm.add_component_to_deployment("acc", "worker", "ghcr.io/org/worker:v2")

        assert "warnings" not in result


class TestAddComponentToDeploymentRefusals:
    async def test_an_unknown_deployment_is_refused_without_a_commit(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(return_value=_project())

        result = await pm.add_component_to_deployment("nope", "web", "ghcr.io/org/web:v1")

        assert result == {
            "success": False,
            "error": "Deployment 'nope' not found in project 'demo'",
            "error_type": "deployment_not_found",
        }
        pm.save_and_commit_project.assert_not_awaited()

    async def test_an_unknown_component_is_refused_without_a_commit(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(return_value=_project())

        result = await pm.add_component_to_deployment("prod", "nope", "ghcr.io/org/nope:v1")

        assert result == {
            "success": False,
            "error": "Component 'nope' not found in project 'demo'",
            "error_type": "component_not_found",
        }
        pm.save_and_commit_project.assert_not_awaited()

    async def test_a_component_already_in_the_deployment_is_refused(self) -> None:
        pm = _manager()
        project_data = _project()
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.add_component_to_deployment("prod", "web", "ghcr.io/org/web:v2")

        assert result == {
            "success": False,
            "error": "Component 'web' is already in deployment 'prod'",
            "error_type": "duplicate_component_in_deployment",
        }
        assert len(project_data["deployments"][0]["components"]) == 1
        pm.save_and_commit_project.assert_not_awaited()

    async def test_a_failing_save_leaks_nothing_to_the_caller(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(return_value=_project())
        pm.save_and_commit_project = AsyncMock(side_effect=RuntimeError("push refused, /path/to/secret"))

        result = await pm.add_component_to_deployment("acc", "worker", "ghcr.io/org/worker:v2")

        assert result == {
            "success": False,
            "error": "An internal error occurred",
            "error_type": "internal_error",
        }
