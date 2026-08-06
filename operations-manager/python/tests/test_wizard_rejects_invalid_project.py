"""The wizard must not hand an invalid project file to the storage layer.

Background: a project whose ``command`` was left empty was written as
``command: []``, which the schema rejects (``minItems: 1``). Nothing in the
wizard checked the finished file, so the rejection first surfaced from the git
step of the background task -- after the wizard session had already been thrown
away, leaving the user on a dead-end page with the raw schema message and no way
back to their input.

These tests pin the three things that fixes it:

- the finished file is schema-checked while the wizard still exists, on BOTH
  paths (create and edit), before anything is handed to storage;
- the create flow keeps its wizard session until the work is accepted;
- a schema violation is placed on the step and field it came from, with a
  step-level fallback when it cannot be placed.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.core.project_schema import ProjectSchemaError
from opi.forms.visualizers.flows import get_flow
from opi.web.router_wizard import (
    _locate_schema_error,
    _save_existing_project,
    _schema_path_to_editable_path,
    _start_project_creation,
    _validate_finished_project,
)

AGE_BLOB = "-----BEGIN AGE ENCRYPTED FILE-----\nfake\n-----END AGE ENCRYPTED FILE-----"

OWNER_EMAIL = "owner@example.com"


def _valid_project(name: str = "nep-wfo") -> dict[str, Any]:
    """A minimal project file that passes the schema."""
    return {
        "schema-version": 2.6,
        "name": name,
        "display-name": "Nep WFO",
        "clusters": ["odcn-production"],
        "users": [{"email": OWNER_EMAIL, "role": "owner"}],
        "components": [{"name": "frontend", "image": "nginx:latest"}],
        "config": {
            "api-key": AGE_BLOB,
            "age-public-key": "age1publicpublicpublic",
            "age-private-key": AGE_BLOB,
        },
    }


def _project_with_empty_command(name: str = "nep-wfo") -> dict[str, Any]:
    """The real-world offender: a start command left empty writes an empty list."""
    data = _valid_project(name)
    data["components"][0]["command"] = []
    return data


def _request_for(email: str) -> Any:
    """Minimal fake request whose state.user yields the given email."""
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}))


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


def test_valid_project_passes() -> None:
    _validate_finished_project(_valid_project(), project_name="nep-wfo")


def test_empty_command_is_rejected_with_its_field_path() -> None:
    """The violation that started this: ``command: []`` against ``minItems: 1``."""
    with pytest.raises(ProjectSchemaError) as exc:
        _validate_finished_project(_project_with_empty_command(), project_name="nep-wfo")

    assert exc.value.field_path == "components/0/command"
    assert "components/0/command" in str(exc.value)


def test_rejection_is_logged_as_a_form_bug_with_the_field_path(caplog: pytest.LogCaptureFixture) -> None:
    """It is a bug when the wizard builds this, so it must be findable in the log."""
    with caplog.at_level("WARNING", logger="opi.web.router_wizard"), pytest.raises(ProjectSchemaError):
        _validate_finished_project(_project_with_empty_command(), project_name="nep-wfo")

    assert any("components/0/command" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Create path: the failure happens in the wizard, not in the git step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_invalid_project_before_any_task_is_created() -> None:
    """No async task, so the git step never sees it and never reports it."""
    create_task = AsyncMock(return_value={"task_id": "should-not-happen"})
    clear_state = patch("opi.web.router_wizard.clear_wizard_state").start()

    try:
        with (
            patch("opi.core.task_helpers.create_async_task", create_task),
            pytest.raises(ProjectSchemaError) as exc,
        ):
            await _start_project_creation(_request_for(OWNER_EMAIL), _project_with_empty_command())
    finally:
        patch.stopall()

    assert exc.value.field_path == "components/0/command"
    create_task.assert_not_awaited()
    # The session that holds everything the user typed is still there.
    clear_state.assert_not_called()


@pytest.mark.asyncio
async def test_create_clears_the_session_only_after_the_task_exists() -> None:
    """Order matters: a session cleared first is gone when the submission fails."""
    calls: list[str] = []

    async def _create(**_kwargs: Any) -> dict[str, Any]:
        calls.append("create_async_task")
        return {"task_id": "task-1"}

    with (
        patch("opi.core.task_helpers.create_async_task", _create),
        patch("opi.web.router_wizard.clear_wizard_state", lambda _request: calls.append("clear_wizard_state")),
    ):
        response = await _start_project_creation(_request_for(OWNER_EMAIL), _valid_project())

    assert response.headers["HX-Redirect"] == "/projects/progress/task-1"
    assert calls == ["create_async_task", "clear_wizard_state"]


@pytest.mark.asyncio
async def test_create_keeps_the_session_when_the_task_cannot_be_created() -> None:
    """A failed hand-over must leave the user with their input intact."""
    clear_state = patch("opi.web.router_wizard.clear_wizard_state").start()

    async def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("task service unavailable")

    try:
        with patch("opi.core.task_helpers.create_async_task", _boom), pytest.raises(RuntimeError):
            await _start_project_creation(_request_for(OWNER_EMAIL), _valid_project())
    finally:
        patch.stopall()

    clear_state.assert_not_called()


# ---------------------------------------------------------------------------
# Edit path: same check, same moment (right before storage)
# ---------------------------------------------------------------------------


def _patch_project_manager(stored: dict[str, Any], captured: dict[str, Any]):
    class _FakeProjectManager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_contents(self) -> dict[str, Any]:
            return copy.deepcopy(stored)

        async def save_and_commit_project(self, project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
            captured["data"] = project_data

        async def close(self) -> None:
            pass

    return patch("opi.manager.project_manager.ProjectManager", _FakeProjectManager)


@pytest.mark.asyncio
async def test_edit_rejects_invalid_project_before_saving() -> None:
    """The edit path validates the MERGED file: the form alone is only a subset."""
    captured: dict[str, Any] = {}
    stored = _valid_project()

    with (
        patch("opi.web.project_edit_security.require_project_edit_access"),
        _patch_project_manager(stored, captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
        pytest.raises(ProjectSchemaError) as exc,
    ):
        await _save_existing_project(
            _request_for(OWNER_EMAIL),
            stored["name"],
            {"components": [{"name": "frontend", "image": "nginx:latest", "command": []}]},
        )

    assert exc.value.field_path == "components/0/command"
    assert "data" not in captured


@pytest.mark.asyncio
async def test_edit_saves_a_valid_project() -> None:
    """The check is a gate, not a wall: a valid edit still goes through."""
    captured: dict[str, Any] = {}
    stored = _valid_project()

    with (
        patch("opi.web.project_edit_security.require_project_edit_access"),
        _patch_project_manager(stored, captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), stored["name"], {"display-name": "Andere naam"})

    assert captured["data"]["display-name"] == "Andere naam"


# ---------------------------------------------------------------------------
# Placing the message: from schema field path to step and field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema_path", "expected"),
    [
        ("components/0/command", "components[0]/command"),
        ("users/2/email", "users[2]/email"),
        ("display-name", "display-name"),
        ("deployments/0/components/1/image", "deployments[0]/components[1]/image"),
    ],
)
def test_schema_path_becomes_an_editable_path(schema_path: str, expected: str) -> None:
    assert _schema_path_to_editable_path(schema_path) == expected


def test_schema_error_is_placed_on_the_step_that_owns_the_field() -> None:
    sections = get_flow("create-project").sections
    located = _locate_schema_error(list(sections), "components/0/command")

    assert located is not None
    section, editable_path = located
    assert section.section_id == "components"
    assert editable_path == "components[0]/command"


def test_unplaceable_schema_error_returns_none() -> None:
    """No step owns this, so the caller falls back to a step-level message."""
    sections = get_flow("create-project").sections
    assert _locate_schema_error(list(sections), "config/age-private-key") is None
