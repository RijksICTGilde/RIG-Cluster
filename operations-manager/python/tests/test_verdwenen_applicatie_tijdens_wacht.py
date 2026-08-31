"""Een deployment die tijdens de wacht verdwijnt is geen mislukte uitrol.

Op 31 augustus 2026 gaf ``mpfb-8wh`` twee keer binnen een half uur
``timed out after 300s waiting for sync``. Er was niets mis met de uitrol: een
gelijktijdige ``delete_deployment`` voor ``pr-244`` had de Application om 09:21:42
uit de argo-repo gehaald, en de projectbrede taak begon er om 09:22:02 op te wachten.
ArgoCD antwoordde vanaf 09:22:16 dat de app niet bestaat, en de wachtlus las dat 144
keer als een tijdelijke leesfout tot de time-out vol was.

Deze test houdt de uitkomst vast: de wacht meldt ``ApplicationGone``, de deployment
komt als ``removed`` uit ``_refresh_and_wait``, en de verwerking daarvan levert nul
sync-fouten op - de run slaagt.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_manager import TaskStatus
from opi.manager.argo_manager import ApplicationGone

DEPLOYMENT = {
    "name": "pr-244",
    "namespace": "mpfb-8wh",
    "cluster": "odcn-production",
    "components": [],
}


def _progress() -> Any:
    """Een echte voortgangsmanager zonder zijn periodieke flush-lus."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-under-test",
            project_name="mpfb-8wh",
            task_service=MagicMock(),
        )


def _project_manager(monkeypatch: pytest.MonkeyPatch, *, wacht_fout: BaseException) -> Any:
    """Een ProjectManager die tot aan de ArgoCD-wacht loopt en daar ``wacht_fout`` krijgt."""
    from opi.manager.project_manager import ProjectManager

    manager = ProjectManager(project_file_relative_path="projects/mpfb-8wh.yaml")

    current_yaml = {"name": "mpfb-8wh", "deployments": [DEPLOYMENT]}
    analysis = {
        "current_yaml": current_yaml,
        "previous_yaml": None,
        "changes": {"added": {}, "changed": {}, "deleted": {}},
    }

    handler = manager._project_file_handler
    monkeypatch.setattr(handler, "analyze_project_changes", AsyncMock(return_value=analysis))
    monkeypatch.setattr(type(handler), "was_migrated", property(lambda self: False), raising=False)

    monkeypatch.setattr(manager, "get_git_connector_for_project_files", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("opi.manager.project_manager.validate_project_schema", lambda *a, **k: None)
    monkeypatch.setattr(manager, "save_and_commit_project", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "process_project", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "get_deployments", AsyncMock(return_value=[DEPLOYMENT]))
    monkeypatch.setattr(manager, "get_name", AsyncMock(return_value="mpfb-8wh"))
    monkeypatch.setattr(manager, "get_contents", AsyncMock(return_value=current_yaml))
    monkeypatch.setattr(manager, "close", AsyncMock(return_value=None))

    # De Application bestaat al, dus geen umbrella-refresh en geen wacht-tot-aangemaakt.
    monkeypatch.setattr(
        manager._kubectl_connector, "argocd_application_exists", AsyncMock(return_value=True), raising=False
    )

    argo_connector = MagicMock()
    argo_connector.refresh_application = AsyncMock(return_value="2026-08-31T09:22:02Z")
    argo_connector.get_application_manifests = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr("opi.manager.project_manager.create_argo_connector", lambda *a, **k: argo_connector)

    monkeypatch.setattr(
        manager._argo_manager, "wait_for_application_synced", AsyncMock(side_effect=wacht_fout), raising=False
    )
    return manager


def _subtasks(progress: Any) -> list[tuple[str, str]]:
    return [(info["name"], info["status"]) for info in progress._subtasks.values()]


@pytest.mark.asyncio
async def test_verdwenen_applicatie_levert_geen_syncfout_op(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _project_manager(monkeypatch, wacht_fout=ApplicationGone("Application 'mpfb-8wh-pr-244' verdween"))
    progress = _progress()

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is True, manager.get_processing_error()
    assert manager.get_processing_error() is None, "een verwijderde deployment is geen fout van deze taak"

    stappen = dict(_subtasks(progress))
    assert stappen["pr-244"] == TaskStatus.COMPLETED.value, _subtasks(progress)
    onderwerpen = [info.get("subject") for info in progress._subtasks.values() if info["name"] == "pr-244"]
    assert onderwerpen == ["verwijderd tijdens de uitrol"], onderwerpen


@pytest.mark.asyncio
async def test_een_echte_syncfout_blijft_wel_falen(monkeypatch: pytest.MonkeyPatch) -> None:
    """De tegenproef: alleen ApplicationGone is onschuldig, een gewone RuntimeError niet."""
    manager = _project_manager(monkeypatch, wacht_fout=RuntimeError("Application 'mpfb-8wh-pr-244' is degraded"))
    progress = _progress()

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is False
    assert "is degraded" in (manager.get_processing_error() or "")
