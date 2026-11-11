"""Characterization tests for ``ProjectManager.clear_service_config``.

These record the behavior as it IS, down to the dict handed to ``save_and_commit_project``
and the commit message, rather than the behavior one might prefer.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_store import MutationResult
from opi.services.services import ServiceValidationError


def _manager() -> ProjectManager:
    pm = ProjectManager.__new__(ProjectManager)
    pm._project_file_handler = MagicMock()
    pm._ProjectManager__has_contents = True
    pm.get_name = AsyncMock(return_value="demo")
    # The real return value: ``committed`` is what tells the caller whether to roll out.
    pm.save_and_commit_project = AsyncMock(
        return_value=MutationResult(before=None, after={}, ref="test", committed=True)
    )
    return pm


def _project(services: list[Any] | None = None, components: list[Any] | None = None) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": services if services is not None else [],
        "components": components if components is not None else [],
    }


class TestClearServiceConfigProjectLayer:
    async def test_config_is_demoted_to_a_bare_string_and_committed(self) -> None:
        pm = _manager()
        project_data = _project(services=[{"name": "auth-wall", "config": {"allowed-emails": ["a@b.nl"]}}])
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.clear_service_config("auth-wall", "project")

        assert result == {
            "success": True,
            "service": "auth-wall",
            "target": "project",
            "removed": True,
            "changed": True,
        }
        assert project_data["services"] == ["auth-wall"]
        saved, message = pm.save_and_commit_project.await_args.args
        assert saved is project_data
        assert message == "Clear service 'auth-wall' config at project target in project 'demo'"

    async def test_an_already_bare_string_is_a_noop_without_a_commit(self) -> None:
        pm = _manager()
        project_data = _project(services=["auth-wall"])
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.clear_service_config("auth-wall", "project")

        assert result == {
            "success": True,
            "service": "auth-wall",
            "target": "project",
            "removed": False,
            "changed": False,
        }
        assert project_data["services"] == ["auth-wall"]
        pm.save_and_commit_project.assert_not_awaited()

    async def test_an_absent_service_is_a_noop_without_a_commit(self) -> None:
        pm = _manager()
        project_data = _project(services=[])
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.clear_service_config("auth-wall", "project")

        assert result["removed"] is False
        pm.save_and_commit_project.assert_not_awaited()


class TestClearServiceConfigComponentLayer:
    async def test_config_on_a_component_is_demoted_to_a_bare_string(self) -> None:
        pm = _manager()
        project_data = _project(
            components=[{"name": "web", "services": [{"name": "auth-wall", "config": {"allowed-emails": []}}]}]
        )
        pm.get_contents = AsyncMock(return_value=project_data)

        result = await pm.clear_service_config("auth-wall", "component", component_name="web")

        assert result["removed"] is True
        assert project_data["components"][0]["services"] == ["auth-wall"]
        assert (
            pm.save_and_commit_project.await_args.args[1]
            == "Clear service 'auth-wall' config at component target in project 'demo'"
        )


class TestClearServiceConfigErrorPaths:
    async def test_an_unknown_target_is_refused_before_the_first_read(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(side_effect=AssertionError("must not be read"))

        result = await pm.clear_service_config("auth-wall", "nonsense")

        assert result == {"success": False, "error": "Unknown target 'nonsense'", "error_type": "invalid_target"}
        pm.save_and_commit_project.assert_not_awaited()

    async def test_an_adapter_that_rejects_the_target_yields_invalid_target(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(return_value=_project())

        with patch(
            "opi.manager.project.service_config_mutations.ServiceAdapter.remove_service_config",
            side_effect=ServiceValidationError("component_name is required"),
        ):
            result = await pm.clear_service_config("auth-wall", "component")

        assert result == {
            "success": False,
            "error": "component_name is required",
            "error_type": "invalid_target",
        }
        pm.save_and_commit_project.assert_not_awaited()

    @pytest.mark.parametrize("failure", [ProjectSchemaError("broken schema"), ProjectIntegrityError("broken ref")])
    async def test_a_rejected_save_yields_validation_error(self, failure: Exception) -> None:
        pm = _manager()
        project_data = _project(services=[{"name": "auth-wall", "config": {"allowed-emails": []}}])
        pm.get_contents = AsyncMock(return_value=project_data)
        pm.save_and_commit_project = AsyncMock(side_effect=failure)

        result = await pm.clear_service_config("auth-wall", "project")

        assert result == {"success": False, "error": str(failure), "error_type": "validation_error"}

    async def test_an_unexpected_failure_leaks_nothing_to_the_caller(self) -> None:
        pm = _manager()
        pm.get_contents = AsyncMock(side_effect=RuntimeError("git gone, with /path/to/secret in it"))

        result = await pm.clear_service_config("auth-wall", "project")

        assert result == {
            "success": False,
            "error": "An internal error occurred",
            "error_type": "internal_error",
        }
