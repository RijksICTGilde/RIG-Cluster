"""The values use case: what it hands the writer, and what it does with the answer."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.manager.service.errors import InvalidProjectNameError, InvalidValuesError
from opi.manager.service.values import ComponentValuesChange, set_component_values
from opi.services.catalog.base import ConfigLayer
from opi.services.component_values import ValuesOperation
from opi.services.tasks.task_rollout import SKIPPED_REASON

pytestmark = pytest.mark.asyncio


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name, subject=None: f"task-{name}"
    return progress


def _manager(write: dict[str, Any], *, processed: bool = True) -> MagicMock:
    manager = MagicMock()
    manager.set_component_values = AsyncMock(return_value=write)
    manager.process_project_from_git = AsyncMock(return_value=processed)
    manager.get_processing_error = MagicMock(return_value=None)
    return manager


def _change(**overrides: Any) -> ComponentValuesChange:
    fields: dict[str, Any] = {
        "project_name": "demo",
        "service_name": "user-env-vars",
        "layer": ConfigLayer.COMPONENT,
        "operation": ValuesOperation.REPLACE,
        "component_name": "web",
        "values": {"A": "1"},
    }
    return ComponentValuesChange(**{**fields, **overrides})


async def test_the_writer_gets_the_layer_and_the_operation_as_their_values() -> None:
    manager = _manager({"success": True, "changed": True})

    await set_component_values(
        _change(layer=ConfigLayer.DEPLOYMENT_COMPONENT, deployment_name="prod"), manager, _progress()
    )

    call = manager.set_component_values.await_args
    assert call.args == ("user-env-vars", "deployment-component", "replace")
    assert call.kwargs["component_name"] == "web"
    assert call.kwargs["deployment_name"] == "prod"
    assert call.kwargs["values"] == {"A": "1"}


async def test_an_impossible_project_name_never_reaches_the_write() -> None:
    manager = _manager({"success": True})

    with pytest.raises(InvalidProjectNameError):
        await set_component_values(_change(project_name="Niet Goed"), manager, _progress())

    manager.set_component_values.assert_not_awaited()


async def test_a_refused_value_is_an_invalid_values_error() -> None:
    manager = _manager({"success": False, "error": "randwitruimte", "error_type": "invalid_values"})

    with pytest.raises(InvalidValuesError) as refusal:
        await set_component_values(_change(), manager, _progress())

    assert str(refusal.value) == "randwitruimte"
    manager.process_project_from_git.assert_not_called()


async def test_a_change_processes_the_deployments_using_the_component() -> None:
    manager = _manager({"success": True, "changed": True})

    result = await set_component_values(_change(deployment_names=["prod", "test"]), manager, _progress())

    assert manager.process_project_from_git.await_args.kwargs["deployment_names"] == ["prod", "test"]
    assert result.changed is True
    assert result.succeeded


async def test_an_unchanged_set_processes_nothing() -> None:
    manager = _manager({"success": True, "changed": False})

    result = await set_component_values(_change(), manager, _progress())

    manager.process_project_from_git.assert_not_called()
    assert result.changed is False
    assert result.processing == {"status": "skipped"}


async def test_rollout_false_leaves_the_rollout_to_the_caller() -> None:
    manager = _manager({"success": True, "changed": True})

    result = await set_component_values(_change(rollout=False), manager, _progress())

    manager.process_project_from_git.assert_not_called()
    assert result.processing["reason"] == SKIPPED_REASON
