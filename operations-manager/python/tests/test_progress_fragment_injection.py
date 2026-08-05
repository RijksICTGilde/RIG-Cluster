"""The shared progress fragment is rendered once, and only about things the project has.

Two halves of the same defect, found in the security review of RC-29:

1. The fragment used to be rendered and then run through ``process_components``, which
   parses already-rendered HTML as a Jinja template. Autoescape escapes ``< > & " '``
   but not ``{{``, so any text that reached the fragment -- a step name, a success
   message, a subtask name -- was executed instead of shown. That is code execution in
   the OPI pod, reachable by anyone who may confirm an action on their own project.
2. The text in that fragment comes from the URL: the routes put the attachment id,
   component name and deployment name straight into the step name without ever checking
   them against the project, so an id the project does not have still reached the render.

The first fix is what makes it safe; the second keeps made-up names out of the task in
the first place (and gives the user a 404 at the click instead of a task that fails).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

PAYLOAD = "{{ 7 * 7 }}"


def _context(**overrides) -> dict:
    context = {
        "task_id": "t-1",
        "progress_url": "/projects/demo/task-progress/t-1",
        "progress": 0,
        "current_step": PAYLOAD,
        "tasks": [{"name": PAYLOAD, "status": "running", "subtasks": [{"name": PAYLOAD, "status": "running"}]}],
        "status": "running",
        "success_message": PAYLOAD,
        "on_complete": "location.reload()",
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# 1. rendered once
# ---------------------------------------------------------------------------


def test_text_in_the_fragment_is_shown_not_executed() -> None:
    from opi.web.task_progress import render_progress_fragment

    rendered = render_progress_fragment(_context())

    assert "49" not in rendered
    assert rendered.count("7 * 7") == 3  # step, task name, subtask name


def test_a_success_message_is_shown_not_executed() -> None:
    from opi.web.task_progress import render_progress_fragment

    rendered = render_progress_fragment(_context(status="completed"))

    assert "49" not in rendered
    assert "7 * 7" in rendered


def test_the_single_render_still_resolves_every_component() -> None:
    """Dropping the second pass may not leave raw ``<c-...>`` tags in the answer.

    The extension replaces them when the template file is compiled, which is why the
    second pass was never needed here; this is the measurement of that claim.
    """
    from opi.web.task_progress import render_progress_fragment

    for status in ("running", "completed", "failed"):
        rendered = render_progress_fragment(_context(status=status, current_step="stap"))
        assert "<c-" not in rendered, f"unresolved component tag with status={status}"


# ---------------------------------------------------------------------------
# 2. only about things the project has
# ---------------------------------------------------------------------------


def _request(email: str = "admin@example.com") -> MagicMock:
    request = MagicMock()
    request.state.user = {"email": email}
    return request


def _project(data: dict) -> MagicMock:
    project = MagicMock()
    project.data = data
    return project


def _store(data: dict) -> MagicMock:
    store = MagicMock()
    store.get.return_value = _project(data)
    return store


PROJECT = {
    "name": "demo",
    "components": [{"name": "web"}],
    "deployments": [{"name": "prod"}],
    "services": [{"attachments": {"data": [{"id": "a1", "filename": "logo.png"}]}}],
}


async def test_deleting_a_component_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router

    with (
        patch.object(router, "get_project_store", return_value=_store(PROJECT)),
        patch.object(router, "is_user_authorized_for_project", return_value=True),
        patch.object(router, "get_user_role_for_project", return_value="admin"),
        patch.object(router, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router.delete_component_web(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_reprocessing_a_deployment_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router

    with (
        patch.object(router, "get_project_store", return_value=_store(PROJECT)),
        patch.object(router, "is_user_authorized_for_project", return_value=True),
        patch.object(router, "get_user_role_for_project", return_value="admin"),
        patch.object(router, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router.refresh_deployment_web(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_deleting_an_attachment_the_project_does_not_have_is_a_404() -> None:
    from opi.web import router_attachments

    with (
        patch.object(router_attachments, "require_project_edit_access", return_value=(_project(PROJECT), "a@b.c")),
        patch.object(router_attachments, "create_task_and_render_progress", new=AsyncMock()) as create,
        pytest.raises(HTTPException) as excinfo,
    ):
        await router_attachments.delete_attachment(_request(), "demo", PAYLOAD)

    assert excinfo.value.status_code == 404
    create.assert_not_awaited()


async def test_an_attachment_the_project_does_have_starts_the_task() -> None:
    from opi.web import router_attachments

    with (
        patch.object(router_attachments, "require_project_edit_access", return_value=(_project(PROJECT), "a@b.c")),
        patch.object(router_attachments, "create_task_and_render_progress", new=AsyncMock()) as create,
    ):
        await router_attachments.delete_attachment(_request(), "demo", "a1")

    create.assert_awaited_once()


# ---------------------------------------------------------------------------
# the progress fragment is scoped to its project
# ---------------------------------------------------------------------------


async def test_a_task_of_another_project_is_not_readable_through_this_project() -> None:
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(return_value={"task_id": "t-1", "project_name": "other"})

    with patch.object(router, "is_user_authorized_for_project", return_value=True):
        task = await router._require_task_of_project(_request(), task_service, "demo", "t-1")

    assert task is None


async def test_a_project_you_may_not_see_gives_403_on_task_progress() -> None:
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(return_value={"task_id": "t-1", "project_name": "demo"})

    with (
        patch.object(router, "is_user_authorized_for_project", return_value=False),
        pytest.raises(HTTPException) as excinfo,
    ):
        await router._require_task_of_project(_request(), task_service, "demo", "t-1")

    assert excinfo.value.status_code == 403


async def test_whoever_started_the_task_may_follow_it_to_the_end() -> None:
    """A delete removes the project from the store, so the poll that reports "klaar"
    arrives when the user is no longer a member of anything. Scoping on the project
    alone would 403 exactly the task that needed watching."""
    from opi.web import router

    task_service = MagicMock()
    task_service.get_task = AsyncMock(
        return_value={"task_id": "t-1", "project_name": "demo", "created_by": "Admin@Example.com"}
    )

    with patch.object(router, "is_user_authorized_for_project", return_value=False) as authorized:
        task = await router._require_task_of_project(_request("admin@example.com"), task_service, "demo", "t-1")

    assert task is not None
    authorized.assert_not_called()
