"""Regression tests for handle_delete_deployment honesty.

Previously a partially-failed delete returned a "partial" result and the
top-level task reported "completed successfully" - so the nightly cleaner was
told the deployment was gone while resources stayed behind, and orphaned
previews accumulated. The task must now FAIL when the delete is not fully
successful, so the caller/cleaner retries (the delete is idempotent).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_handlers_deployment import handle_delete_deployment


def _progress() -> MagicMock:
    p = MagicMock()
    p.add_task = MagicMock(return_value="task-handle")
    return p


def _pm_with_result(result: dict) -> MagicMock:
    pm = MagicMock()
    pm.delete_deployment = AsyncMock(return_value=result)
    pm.close = AsyncMock()
    return pm


@pytest.mark.asyncio
async def test_partial_failure_raises_so_task_fails() -> None:
    pm = _pm_with_result({"success": False, "errors": ["ArgoCD app delete failed"], "operations": []})
    progress = _progress()
    with (
        patch("opi.manager.project_manager.create_project_manager", return_value=pm),
        pytest.raises(RuntimeError, match="not fully deleted"),
    ):
        await handle_delete_deployment({"project_name": "p", "deployment_name": "d"}, progress)
    progress.fail_project.assert_called()  # task failure propagated, not swallowed


@pytest.mark.asyncio
async def test_full_success_returns_completed() -> None:
    pm = _pm_with_result({"success": True, "errors": [], "operations": []})
    progress = _progress()
    with patch("opi.manager.project_manager.create_project_manager", return_value=pm):
        result = await handle_delete_deployment({"project_name": "p", "deployment_name": "d"}, progress)
    assert result["status"] == "completed"
    progress.fail_project.assert_not_called()
