"""The service-config use case: the steps it reports and the failures it raises.

Every entrance of this change goes through ``configure_service``, so what it does on a
refusal, on a no-op and on a failed rollout is what every entrance does.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.manager.service.configure import (
    INVALID_PROJECT_NAME,
    ConfigOperation,
    ServiceConfigChange,
    configure_service,
)
from opi.manager.service.errors import (
    ChangeConflictError,
    ChangeValidationError,
    InvalidProjectNameError,
    InvalidTargetError,
    ServiceChangeError,
)
from opi.services.tasks.task_rollout import SKIPPED_REASON

pytestmark = pytest.mark.asyncio


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name, subject=None: f"task-{name}"
    return progress


def _manager(write: dict[str, Any], *, processed: bool = True, error: str | None = None) -> MagicMock:
    manager = MagicMock()
    manager.configure_service = AsyncMock(return_value=write)
    manager.clear_service_config = AsyncMock(return_value=write)
    manager.patch_service_config_list = AsyncMock(return_value=write)
    manager.process_project_from_git = AsyncMock(return_value=processed)
    manager.get_processing_error = MagicMock(return_value=error)
    return manager


def _change(**overrides: Any) -> ServiceConfigChange:
    fields: dict[str, Any] = {
        "project_name": "demo",
        "service_name": "keycloak",
        "target": "project",
        "operation": ConfigOperation.UPSERT,
        "config": {"template": "x"},
    }
    return ServiceConfigChange(**{**fields, **overrides})


class TestRefusals:
    async def test_an_impossible_project_name_never_reaches_the_write(self) -> None:
        manager = _manager({"success": True})
        progress = _progress()

        with pytest.raises(InvalidProjectNameError) as refusal:
            await configure_service(_change(project_name="Niet Goed"), manager, progress)

        assert str(refusal.value) == INVALID_PROJECT_NAME
        manager.configure_service.assert_not_awaited()
        progress.fail_project.assert_called_once_with(INVALID_PROJECT_NAME)

    @pytest.mark.parametrize(
        ("error_type", "expected"),
        [
            ("invalid_target", InvalidTargetError),
            ("validation_error", ChangeValidationError),
            ("conflict", ChangeConflictError),
            ("something_new", ServiceChangeError),
        ],
    )
    async def test_the_writers_error_type_decides_the_exception(
        self, error_type: str, expected: type[Exception]
    ) -> None:
        manager = _manager({"success": False, "error": "nee", "error_type": error_type})

        with pytest.raises(expected) as refusal:
            await configure_service(_change(), manager, _progress())

        assert str(refusal.value) == "nee"
        manager.process_project_from_git.assert_not_called()

    async def test_a_refusal_without_a_message_still_says_something(self) -> None:
        manager = _manager({"success": False})

        with pytest.raises(ServiceChangeError) as refusal:
            await configure_service(_change(), manager, _progress())

        assert str(refusal.value) == "Unknown error configuring service"

    async def test_an_upsert_without_config_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="zonder config"):
            await configure_service(_change(config=None), _manager({"success": True}), _progress())


class TestTheRollout:
    async def test_a_change_processes_the_deployments_the_entrance_resolved(self) -> None:
        manager = _manager({"success": True, "changed": True})

        result = await configure_service(_change(deployment_names=["prod"]), manager, _progress())

        assert manager.process_project_from_git.await_args.args[0] == "projects/demo.yaml"
        assert manager.process_project_from_git.await_args.kwargs["deployment_names"] == ["prod"]
        assert result.processing == {"status": "completed"}
        assert result.succeeded

    async def test_a_no_op_processes_nothing(self) -> None:
        manager = _manager({"success": True, "changed": False})

        result = await configure_service(_change(), manager, _progress())

        manager.process_project_from_git.assert_not_called()
        assert result.changed is False
        assert result.processing == {"status": "skipped"}

    async def test_rollout_false_says_why_it_skipped(self) -> None:
        manager = _manager({"success": True, "changed": True})

        result = await configure_service(_change(rollout=False), manager, _progress())

        manager.process_project_from_git.assert_not_called()
        assert result.processing["reason"] == SKIPPED_REASON

    async def test_a_failed_rollout_fails_the_task_and_carries_its_reason(self) -> None:
        manager = _manager({"success": True, "changed": True}, processed=False, error="argocd zei nee")
        progress = _progress()

        result = await configure_service(_change(), manager, progress)

        assert result.processing == {"status": "failed", "error": "argocd zei nee"}
        assert not result.succeeded
        progress.fail_project.assert_called_once_with("argocd zei nee")

    async def test_a_failed_rollout_without_a_reason_still_fails(self) -> None:
        manager = _manager({"success": True, "changed": True}, processed=False, error=None)
        progress = _progress()

        result = await configure_service(_change(), manager, progress)

        assert result.processing == {"status": "failed"}
        progress.fail_project.assert_called_once_with("Project processing failed")


class TestTheDispatch:
    async def test_clear_goes_to_the_clear_writer_and_reports_removed(self) -> None:
        manager = _manager({"success": True, "changed": True, "removed": True})

        result = await configure_service(_change(operation=ConfigOperation.CLEAR, config=None), manager, _progress())

        manager.clear_service_config.assert_awaited_once()
        manager.configure_service.assert_not_awaited()
        assert result.counts == {"removed": True}

    async def test_patch_goes_to_the_patch_writer_and_reports_its_counts(self) -> None:
        manager = _manager({"success": True, "changed": True, "added": 1, "updated": 0, "removed": 2})

        result = await configure_service(
            _change(operation=ConfigOperation.PATCH, config=None, add=[{"name": "a"}], remove=["b"]),
            manager,
            _progress(),
        )

        assert manager.patch_service_config_list.await_args.kwargs["add"] == [{"name": "a"}]
        assert manager.patch_service_config_list.await_args.kwargs["remove"] == ["b"]
        assert result.counts == {"added": 1, "updated": 0, "removed": 2}
