"""Een reprocess die niet op Synced + Healthy wacht, en wat hij wel blijft doen.

Sleep-mode's ``waking`` is de ene toestand waarin de ArgoCD-applicatie niet Healthy kan
worden: voor de app terug is, is de app niet ready, en daarna is de wekker dat niet. De
wacht liep daar dus altijd af op de 300s-time-out en meldde een sync-fout voor een uitrol
waar niets mis mee was.

Met ``wait_for_healthy=False`` blijft het pushen, de ArgoCD-refresh en de render-controle
staan, zodat een kapotte kustomization nog steeds direct faalt; alleen het wachten gaat
eruit. De standaard blijft aan, en dat is hieronder de tegenproef.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DEPLOYMENT = {
    "name": "pr-689",
    "namespace": "wies",
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
            project_name="wies",
            task_service=MagicMock(),
        )


def _project_manager(monkeypatch: pytest.MonkeyPatch, *, render_ok: bool = True) -> tuple[Any, Any, Any]:
    """Een ProjectManager die tot aan de ArgoCD-wacht loopt; geeft de twee mocks mee."""
    from opi.manager.project_manager import ProjectManager

    manager = ProjectManager(project_file_relative_path="projects/wies.yaml")

    current_yaml = {"name": "wies", "deployments": [DEPLOYMENT]}
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
    monkeypatch.setattr(manager, "get_name", AsyncMock(return_value="wies"))
    monkeypatch.setattr(manager, "get_contents", AsyncMock(return_value=current_yaml))
    monkeypatch.setattr(manager, "close", AsyncMock(return_value=None))

    monkeypatch.setattr(
        manager._kubectl_connector, "argocd_application_exists", AsyncMock(return_value=True), raising=False
    )

    argo_connector = MagicMock()
    argo_connector.refresh_application = AsyncMock(return_value="2026-10-12T11:37:00Z")
    render = (True, "") if render_ok else (False, "rpc error: code = Unknown desc = kustomize build failed: oeps")
    argo_connector.get_application_manifests = AsyncMock(return_value=render)
    monkeypatch.setattr("opi.manager.project_manager.create_argo_connector", lambda *a, **k: argo_connector)

    wacht = AsyncMock(return_value=None)
    monkeypatch.setattr(manager._argo_manager, "wait_for_application_synced", wacht, raising=False)
    return manager, argo_connector, wacht


@pytest.mark.asyncio
async def test_zonder_gezondheidswacht_wordt_er_niet_gewacht_en_niets_gemeld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, argo_connector, wacht = _project_manager(monkeypatch)
    progress = _progress()

    geslaagd = await manager.process_project_from_git(
        "projects/wies.yaml", task_progress_manager=progress, wait_for_healthy=False
    )

    assert geslaagd is True, manager.get_processing_error()
    assert manager.get_processing_error() is None, "een uitrol zonder wacht is geen mislukte uitrol"
    wacht.assert_not_awaited()
    # Wat wel blijft gebeuren: de refresh en de render-controle.
    argo_connector.refresh_application.assert_awaited()
    argo_connector.get_application_manifests.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_kapotte_render_faalt_nog_steeds_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """De render-controle is de reden dat het wegvallen van de wacht niets verbergt."""
    manager, _, wacht = _project_manager(monkeypatch, render_ok=False)

    geslaagd = await manager.process_project_from_git("projects/wies.yaml", wait_for_healthy=False)

    assert geslaagd is False
    assert "Manifest render failed" in (manager.get_processing_error() or "")
    wacht.assert_not_awaited()


@pytest.mark.asyncio
async def test_de_standaard_wacht_nog_steeds_op_gezondheid(monkeypatch: pytest.MonkeyPatch) -> None:
    """De tegenproef: elke andere aanroeper merkt niets van de nieuwe vlag."""
    manager, _, wacht = _project_manager(monkeypatch)

    geslaagd = await manager.process_project_from_git("projects/wies.yaml")

    assert geslaagd is True, manager.get_processing_error()
    wacht.assert_awaited_once()
    assert wacht.await_args.kwargs["app_name"] == "wies-pr-689"
