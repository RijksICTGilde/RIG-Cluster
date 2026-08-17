"""Deleting a deployment that is not there says so (RC-66, bevinding 6).

``DELETE /api/v2/projects/{p}/{deployment}`` reported plain success for a deployment
that never existed. Idempotent deleting is a defensible choice -- the nightly cleaner
depends on it -- but "it is gone" and "it was never here" are different facts, and in a
script the second one reads as confirmation that something was removed.

The behaviour does not change: still success, still no error. The ANSWER changes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.task_handlers_deployment import handle_delete_deployment


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task = MagicMock(return_value="task-handle")
    return progress


def _pm_with_result(result: dict) -> MagicMock:
    pm = MagicMock()
    pm.delete_deployment = AsyncMock(return_value=result)
    pm.close = AsyncMock()
    return pm


@pytest.mark.asyncio
async def test_a_real_delete_reports_that_it_deleted() -> None:
    pm = _pm_with_result({"success": True, "errors": [], "operations": [], "already_absent": False})

    with patch("opi.manager.project_manager.create_project_manager", return_value=pm):
        result = await handle_delete_deployment({"project_name": "p", "deployment_name": "d"}, _progress())

    assert result["status"] == "completed"
    assert result["deleted"] is True
    assert result["already_absent"] is False
    assert "deleted successfully" in result["message"]


@pytest.mark.asyncio
async def test_an_absent_deployment_is_success_that_says_nothing_was_removed() -> None:
    pm = _pm_with_result({"success": True, "errors": [], "operations": [], "already_absent": True})

    with patch("opi.manager.project_manager.create_project_manager", return_value=pm):
        result = await handle_delete_deployment({"project_name": "p", "deployment_name": "weg"}, _progress())

    assert result["status"] == "completed", "still idempotent success"
    assert result["deleted"] is False
    assert result["already_absent"] is True
    assert "niets verwijderd" in result["message"]
    assert "deleted successfully" not in result["message"]


class TestTheManagerMarksIt:
    """Where the fact is established: the 404-in-force-mode branch."""

    @staticmethod
    def _manager() -> MagicMock:
        from opi.manager.delete_project_manager import DeleteProjectManager

        manager = DeleteProjectManager.__new__(DeleteProjectManager)
        project_manager = MagicMock()
        git_connector = MagicMock()
        git_connector.ensure_repo_cloned = AsyncMock()
        project_manager.get_git_connector_for_project_files = AsyncMock(return_value=git_connector)
        project_manager.get_contents = AsyncMock(return_value={"name": "demo"})
        project_manager.get_deployment_by_name = AsyncMock(return_value=None)
        manager.project_manager = project_manager
        return manager

    @pytest.mark.asyncio
    async def test_a_missing_deployment_is_flagged_not_just_successful(self) -> None:
        manager = self._manager()
        store = MagicMock()
        store.get.return_value = MagicMock(filename="demo.yaml")

        with patch("opi.manager.delete_project_manager.get_project_store", return_value=store):
            result = await manager.delete_deployment("demo", "bestaat-niet", force=True)

        assert result["success"] is True
        assert result["already_absent"] is True
        assert any(op.get("status") == "not_found" for op in result["operations"])

    @pytest.mark.asyncio
    async def test_a_non_404_http_error_is_still_a_failure(self) -> None:
        manager = self._manager()
        manager.project_manager.get_contents = AsyncMock(side_effect=HTTPException(status_code=500, detail="stuk"))
        store = MagicMock()
        store.get.return_value = MagicMock(filename="demo.yaml")

        with patch("opi.manager.delete_project_manager.get_project_store", return_value=store):
            result = await manager.delete_deployment("demo", "d", force=True)

        assert result["success"] is False
        assert result["already_absent"] is False
