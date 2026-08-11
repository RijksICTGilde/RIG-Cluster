"""Deleting a project, a component or an attachment runs as a task.

It used to happen inside the web request, which left the browser on an open POST while
git, ArgoCD, the namespace and the databases were torn down -- and let the dialog be
clicked away halfway. These handlers are the work moved out of the request, so what is
guarded here is that a refusal stays a refusal: a delete that did not fully succeed must
FAIL the task, never report success while resources are left behind.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CREATE_PM_PATH = "opi.manager.project_manager.create_project_manager"
PM_PATH = "opi.manager.project_manager.ProjectManager"


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    return progress


# ---------------------------------------------------------------------------
# handle_delete_project
# ---------------------------------------------------------------------------


async def test_project_delete_completes_when_the_teardown_succeeded() -> None:
    from opi.core.task_handlers_project import handle_delete_project

    progress = _progress()
    pm = AsyncMock()
    pm.delete_project = AsyncMock(return_value={"success": True})

    with patch(CREATE_PM_PATH, return_value=pm):
        result = await handle_delete_project({"project_name": "demo"}, progress)

    pm.delete_project.assert_awaited_once_with("demo")
    pm.close.assert_awaited_once()
    assert result["status"] == "completed"
    assert progress.complete_task.call_count == 1
    progress.fail_project.assert_not_called()


async def test_a_partly_failed_project_delete_fails_the_task() -> None:
    """Otherwise a half-deleted project reports as done and its resources linger."""
    from opi.core.task_handlers_project import handle_delete_project

    progress = _progress()
    pm = AsyncMock()
    pm.delete_project = AsyncMock(return_value={"success": False, "errors": ["namespace stuck"]})

    with patch(CREATE_PM_PATH, return_value=pm), pytest.raises(RuntimeError, match="namespace stuck"):
        await handle_delete_project({"project_name": "demo"}, progress)

    progress.fail_project.assert_called_once()


async def test_deployments_on_another_cluster_say_so() -> None:
    """This instance only manages its own cluster, so it cannot finish the job; the
    message must name the clusters instead of reading as a plain error."""
    from opi.core.task_handlers_project import handle_delete_project

    progress = _progress()
    pm = AsyncMock()
    pm.delete_project = AsyncMock(
        return_value={"success": False, "remaining_deployments": [{"cluster": "odcn-production"}]}
    )

    with patch(CREATE_PM_PATH, return_value=pm), pytest.raises(RuntimeError, match="odcn-production"):
        await handle_delete_project({"project_name": "demo"}, progress)


# ---------------------------------------------------------------------------
# handle_delete_component
# ---------------------------------------------------------------------------


async def test_component_delete_also_reprocesses_the_project() -> None:
    """Removing it from the project file is half the job; without the reprocess the
    component keeps running. Both used to be separate, and the dialog reported success
    after the first half."""
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(return_value={"success": True})
    refresh = AsyncMock(return_value={"status": "success", "processing": {"status": "completed"}})

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.core.task_handlers_operations.handle_refresh_project", refresh),
    ):
        result = await handle_delete_component({"project_name": "demo", "component_name": "web"}, progress)

    pm.delete_component.assert_awaited_once_with("web", confirm_in_use=False)
    pm.close.assert_awaited_once()
    assert refresh.await_args.args[0] == {"project_name": "demo", "force_clone": True}
    assert result["status"] == "completed"


async def test_a_failed_reprocess_after_a_component_delete_stays_visible() -> None:
    """The reprocess reports failure by returning, not by raising."""
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(return_value={"success": True})
    refresh = AsyncMock(return_value={"status": "failed", "message": "kapot", "processing": {"status": "failed"}})

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.core.task_handlers_operations.handle_refresh_project", refresh),
    ):
        result = await handle_delete_component({"project_name": "demo", "component_name": "web"}, progress)

    assert result["status"] == "failed"
    assert result["message"] == "kapot"


async def test_the_confirmation_travels_to_the_write_layer() -> None:
    """The flag is the caller's statement that they have seen what the deletion takes with
    it. A handler that dropped it would either refuse every deletion the portal starts or
    clean up references nobody confirmed."""
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(return_value={"success": True, "uncoupled_from": [{"label": "deployment 'x'"}]})
    refresh = AsyncMock(return_value={"status": "success", "processing": {"status": "completed"}})

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.core.task_handlers_operations.handle_refresh_project", refresh),
    ):
        result = await handle_delete_component(
            {"project_name": "demo", "component_name": "web", "confirm_in_use": True}, progress
        )

    assert pm.delete_component.await_args.kwargs == {"confirm_in_use": True}
    # What went with it, so the caller is not left guessing which deployments changed.
    assert result["uncoupled_from"] == [{"label": "deployment 'x'"}]


async def test_without_the_confirmation_the_write_layer_hears_no() -> None:
    """Absent means no, not 'unspecified': the guard has to decide on a fact."""
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(return_value={"success": True})
    refresh = AsyncMock(return_value={"status": "success"})

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.core.task_handlers_operations.handle_refresh_project", refresh),
    ):
        await handle_delete_component({"project_name": "demo", "component_name": "web"}, progress)

    assert pm.delete_component.await_args.kwargs == {"confirm_in_use": False}


async def test_a_component_still_in_use_fails_the_task() -> None:
    """The endpoint refuses it up front, but the guard in the manager is what decides --
    the project can have changed between the check and the task running."""
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(
        return_value={"success": False, "error": "Component 'web' is in gebruik door: deployment 'staging'"}
    )

    with patch(PM_PATH, return_value=pm), pytest.raises(RuntimeError, match="in gebruik"):
        await handle_delete_component({"project_name": "demo", "component_name": "web"}, progress)

    progress.fail_project.assert_called_once()


async def test_an_unknown_component_fails_the_task() -> None:
    from opi.core.task_handlers_components import handle_delete_component

    progress = _progress()
    pm = AsyncMock()
    pm.delete_component = AsyncMock(return_value={"success": False, "error": "Component niet gevonden"})

    with patch(PM_PATH, return_value=pm), pytest.raises(RuntimeError, match="niet gevonden"):
        await handle_delete_component({"project_name": "demo", "component_name": "nope"}, progress)

    progress.fail_project.assert_called_once()


# ---------------------------------------------------------------------------
# handle_delete_attachment
# ---------------------------------------------------------------------------


async def test_attachment_delete_completes() -> None:
    from opi.services.catalog.attachments.task import handle_delete_attachment

    progress = _progress()
    pm = AsyncMock()
    pm.remove_attachment = AsyncMock(return_value={"success": True, "changed": True})

    with patch(PM_PATH, return_value=pm):
        result = await handle_delete_attachment({"project_name": "demo", "attachment_id": "keystore"}, progress)

    pm.remove_attachment.assert_awaited_once_with("keystore")
    pm.close.assert_awaited_once()
    assert result["status"] == "completed"
    assert result["changed"] is True


async def test_an_attachment_still_in_use_fails_the_task() -> None:
    """The dialog refuses it up front, but the guard in the catalog is what decides."""
    from opi.services.catalog.attachments.task import handle_delete_attachment

    progress = _progress()
    pm = AsyncMock()
    pm.remove_attachment = AsyncMock(
        return_value={"success": False, "error": "Bijlage 'keystore' is in gebruik", "error_type": "in_use"}
    )

    with patch(PM_PATH, return_value=pm), pytest.raises(RuntimeError, match="in gebruik"):
        await handle_delete_attachment({"project_name": "demo", "attachment_id": "keystore"}, progress)

    progress.fail_project.assert_called_once()
