"""Een verwijderde deployment mag niet terugkomen als zombie (RC-254).

In productie stonden er drie: niet meer in het projectbestand, maar hun ArgoCD
Application en manifestmap stonden nog in git, en ArgoCD hield ze draaiend. Twee
oorzaken, en dit bestand dekt ze beide.

A. ``delete_deployment`` haalt het argo-bestand weg in stap 2 en de deployment pas in
   stap 8, minuten later. Een ``process_project`` in dat venster leest het
   projectbestand nog met de deployment erin en schrijft de Application opnieuw.
B. Stap 2 vong elke fout af en stap 8 liep door, dus na een mislukte push verdween de
   deployment alsnog uit het projectbestand, waarna een retry 404 krijgt. De grendel van
   stap 8 blijft, en sinds RC-265 gooit stap 2 die fout door in plaats van hem af te
   vangen: dan eindigt de taak meteen en probeert de worker het opnieuw.
"""

from __future__ import annotations

import copy
import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.connectors.git import GitPushConflictError
from opi.core.async_task_service import AsyncTaskService
from opi.core.config import settings
from opi.manager.argo_manager import ApplicationGone
from opi.manager.delete_project_manager import DeleteProjectManager
from opi.manager.project_manager import ProjectManager
from opi.utils.naming import (
    generate_argocd_appproject_prefix,
    generate_gitops_argocd_application_path,
    get_output_filename_from_template,
)

if TYPE_CHECKING:
    from pathlib import Path

HERE = settings.CLUSTER_MANAGER


# ---------------------------------------------------------------------------
# Oorzaak A: process_project slaat een deployment met een open delete over
# ---------------------------------------------------------------------------


def _project() -> dict[str, Any]:
    return {
        "name": "demo",
        "components": [{"name": "web"}],
        "deployments": [
            {"name": "a", "cluster": HERE, "namespace": "demo", "components": [{"reference": "web"}]},
            {"name": "b", "cluster": HERE, "namespace": "demo", "components": [{"reference": "web"}]},
        ],
    }


class FakeStore:
    """De committed staat van het projectbestand; genoeg voor een ronde door process_project."""

    def __init__(self, committed: dict[str, Any]) -> None:
        self.committed = copy.deepcopy(committed)

    async def read_path(self, relative_path: str, ref: str = "HEAD") -> dict[str, Any]:
        return copy.deepcopy(self.committed)

    async def save(self, name: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.committed = copy.deepcopy(data)


class RecordingProvisioner:
    """Een dienst die onthoudt voor welke deployments er is geprovisioneerd."""

    def __init__(self) -> None:
        self.deployments: list[str] = []

    async def provision(self, ctx: Any) -> None:
        self.deployments.append(ctx.deployment.get("name"))


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    open_deletes: dict[str, str],
) -> tuple[ProjectManager, RecordingProvisioner]:
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: FakeStore(_project()))
    provisioner = RecordingProvisioner()
    monkeypatch.setattr("opi.manager.project_manager.provisioning_services", lambda: [provisioner])

    service = MagicMock()
    service.find_open_delete_deployments = AsyncMock(return_value=open_deletes)
    monkeypatch.setattr("opi.manager.project_manager.AsyncTaskService", lambda cluster: service)

    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
    pm.has_deployments_for_current_cluster = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pm.check_and_create_namespaces = AsyncMock()  # type: ignore[method-assign]
    pm.check_and_create_sops_secrets_in_namespaces = AsyncMock()  # type: ignore[method-assign]
    pm._ensure_database_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    pm._process_application_manifests = AsyncMock()  # type: ignore[method-assign]
    pm._record_reconciliation = AsyncMock()  # type: ignore[method-assign]
    pm._argo_manager = MagicMock(create_argocd_resources=AsyncMock())
    pm._bootstrap_manager = MagicMock(execute_bootstrap_for_deployment=AsyncMock())
    return pm, provisioner


def _scope_of(call: Any) -> Any:
    return call.kwargs["deployment_names"]


def _project_wide(pm: ProjectManager) -> bool:
    """Wat de reconciliatierij over de scope van deze ronde vastlegt."""
    return pm._record_reconciliation.await_args.args[2]


def _bootstrapped(pm: ProjectManager) -> list[str]:
    return [call.args[1].get("name") for call in pm._bootstrap_manager.execute_bootstrap_for_deployment.await_args_list]


async def test_een_projectbrede_ronde_laat_een_deployment_met_open_delete_weg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm, provisioner = _manager(monkeypatch, {"b": "task-1"})

    assert await pm.process_project() is True

    assert provisioner.deployments == ["a"], "b wordt niet opnieuw geprovisioneerd"
    assert _bootstrapped(pm) == ["a"]
    # De twee schrijvers naar git: de manifestmap en de ArgoCD Application.
    assert _scope_of(pm._process_application_manifests.await_args) == ["a"]
    assert _scope_of(pm._argo_manager.create_argocd_resources.await_args) == ["a"]
    assert _scope_of(pm.check_and_create_namespaces.await_args) == ["a"]
    assert _project_wide(pm) is True, "de aanroeper vroeg om het hele project; de grendel versmalde alleen het werk"


async def test_expliciet_in_de_scope_wordt_hij_ook_overgeslagen(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pm, provisioner = _manager(monkeypatch, {"b": "task-1"})

    with caplog.at_level("INFO", logger="opi.manager.project_manager"):
        assert await pm.process_project(deployment_names=["b"]) is True

    assert provisioner.deployments == []
    assert _scope_of(pm._argo_manager.create_argocd_resources.await_args) == []
    assert any("task-1" in record.getMessage() for record in caplog.records if record.levelname == "INFO"), (
        "de overslagmelding noemt de task-id van de delete"
    )
    assert _project_wide(pm) is False


async def test_zonder_open_delete_wordt_alles_geregenereerd(monkeypatch: pytest.MonkeyPatch) -> None:
    pm, provisioner = _manager(monkeypatch, {})

    assert await pm.process_project() is True

    assert provisioner.deployments == ["a", "b"]
    # Projectbreed blijft projectbreed: None, niet een opsomming.
    assert _scope_of(pm._process_application_manifests.await_args) is None
    assert _scope_of(pm._argo_manager.create_argocd_resources.await_args) is None


async def test_een_delete_van_een_andere_deployment_versmalt_niets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een open delete voor een deployment die hier niet meer staat mag de ronde niet raken."""
    pm, provisioner = _manager(monkeypatch, {"allang-weg": "task-9"})

    assert await pm.process_project() is True

    assert provisioner.deployments == ["a", "b"]
    assert _scope_of(pm._argo_manager.create_argocd_resources.await_args) is None


async def test_een_onbereikbare_takentabel_laat_de_ronde_doorgaan(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    pm, provisioner = _manager(monkeypatch, {})
    service = MagicMock()
    service.find_open_delete_deployments = AsyncMock(side_effect=OperationalError("select", {}, Exception("down")))
    monkeypatch.setattr("opi.manager.project_manager.AsyncTaskService", lambda cluster: service)

    assert await pm.process_project() is True

    assert provisioner.deployments == ["a", "b"]


# ---------------------------------------------------------------------------
# De ArgoCD-wacht van process_project_from_git laat ze ook weg
# ---------------------------------------------------------------------------


def _sync_manager(monkeypatch: pytest.MonkeyPatch, open_deletes: dict[str, str]) -> ProjectManager:
    """Een ronde die tot aan de ArgoCD-wacht loopt; alleen die wacht is nog echt."""
    deployment = {"name": "pr-244", "namespace": "demo", "cluster": HERE, "components": []}
    current_yaml = {"name": "demo", "deployments": [deployment]}
    analysis = {
        "current_yaml": current_yaml,
        "previous_yaml": None,
        "changes": {"added": {}, "changed": {}, "deleted": {}},
    }

    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
    handler = pm._project_file_handler
    monkeypatch.setattr(handler, "analyze_project_changes", AsyncMock(return_value=analysis))
    monkeypatch.setattr(type(handler), "was_migrated", property(lambda self: False), raising=False)
    monkeypatch.setattr("opi.manager.project_manager.validate_project_schema", lambda *a, **k: None)
    monkeypatch.setattr(pm, "get_git_connector_for_project_files", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(pm, "save_and_commit_project", AsyncMock(return_value=True))
    monkeypatch.setattr(pm, "process_project", AsyncMock(return_value=True))
    monkeypatch.setattr(pm, "get_deployments", AsyncMock(return_value=[deployment]))
    monkeypatch.setattr(pm, "get_name", AsyncMock(return_value="demo"))
    monkeypatch.setattr(pm, "get_contents", AsyncMock(return_value=current_yaml))
    monkeypatch.setattr(pm, "close", AsyncMock(return_value=None))
    monkeypatch.setattr(pm._kubectl_connector, "argocd_application_exists", AsyncMock(return_value=True), raising=False)
    argo = MagicMock()
    argo.refresh_application = AsyncMock(return_value="2026-10-09T09:22:02Z")
    argo.get_application_manifests = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr("opi.manager.project_manager.create_argo_connector", lambda *a, **k: argo)
    # ApplicationGone is de onschuldige uitkomst (zie test_verdwenen_applicatie_tijdens_wacht):
    # de wacht stopt daar, zodat deze test de wacht meet en niet de hele uitrol erna.
    monkeypatch.setattr(
        pm._argo_manager,
        "wait_for_application_synced",
        AsyncMock(side_effect=ApplicationGone("Application 'demo-pr-244' verdween")),
        raising=False,
    )

    service = MagicMock()
    service.find_open_delete_deployments = AsyncMock(return_value=open_deletes)
    monkeypatch.setattr("opi.manager.project_manager.AsyncTaskService", lambda cluster: service)
    return pm


async def test_er_wordt_niet_gewacht_op_een_application_die_niet_meer_komt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = _sync_manager(monkeypatch, {"pr-244": "task-1"})

    assert await pm.process_project_from_git("projects/demo.yaml") is True

    pm._argo_manager.wait_for_application_synced.assert_not_awaited()


async def test_zonder_open_delete_wordt_er_nog_steeds_gewacht(monkeypatch: pytest.MonkeyPatch) -> None:
    pm = _sync_manager(monkeypatch, {})

    assert await pm.process_project_from_git("projects/demo.yaml") is True

    pm._argo_manager.wait_for_application_synced.assert_awaited()


async def test_alleen_een_open_delete_telt(orm_db) -> None:
    """De query zelf: pending/claimed/running sluiten uit, completed en failed niet."""
    service = AsyncTaskService(cluster="c1")

    async def _delete_task(deployment: str) -> str:
        row = await service.create_task(
            task_type="delete_deployment",
            project_name="demo",
            deployment_name=deployment,
            cluster="c1",
            payload={"project_name": "demo", "deployment_name": deployment},
        )
        return row["task_id"]

    open_task = await _delete_task("open")
    done = await _delete_task("klaar")
    await service.complete_task(done)
    mislukt = await _delete_task("mislukt")
    await service.fail_task(mislukt, "stuk", attempt_count=3, max_attempts=3)

    found = await service.find_open_delete_deployments("demo")

    assert found == {"open": open_task}
    assert await service.find_open_delete_deployments("ander-project") == {}


async def test_een_andere_taaksoort_sluit_niets_uit(orm_db) -> None:
    service = AsyncTaskService(cluster="c1")
    await service.create_task(
        task_type="upsert_deployment",
        project_name="demo",
        deployment_name="b",
        cluster="c1",
        payload={"image": "nginx:1"},
    )

    assert await service.find_open_delete_deployments("demo") == {}


# ---------------------------------------------------------------------------
# Oorzaak B: stap 8 alleen als git echt leeg is
# ---------------------------------------------------------------------------


def _deployment() -> dict[str, Any]:
    return {
        "name": "pr-42",
        "cluster": "local",
        "namespace": "test-project",
        "repository": "main-repo",
        "components": [{"reference": "web"}],
    }


def _delete_manager() -> tuple[DeleteProjectManager, MagicMock, MagicMock]:
    """Een delete-ronde waarin alles slaagt; de test sloopt er één stap van."""
    pm = AsyncMock()
    pm.get_name = AsyncMock(return_value="test-project")
    pm._kubectl_connector = AsyncMock()
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=True)
    pm._keycloak_manager = AsyncMock()
    pm._keycloak_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    pm._minio_manager = AsyncMock()
    pm._minio_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    pm._ensure_database_manager = AsyncMock()
    pm._ensure_database_manager.return_value.delete_resources_for_deployment = AsyncMock(
        return_value={"operations": [], "errors": []}
    )
    pm._manifest_generator = MagicMock()
    pm._manifest_generator.create_kustomization_files = MagicMock(return_value=True)

    gitops = AsyncMock()
    gitops.get_working_dir = AsyncMock(return_value="/tmp/gitops")

    async def _mutate_and_push(message: str, mutate: Any) -> Any:
        return await mutate()

    gitops.mutate_and_push = AsyncMock(side_effect=_mutate_and_push)
    pm.get_git_connector_for_argocd = AsyncMock(return_value=gitops)

    manifests = AsyncMock()
    manifests.get_working_dir = AsyncMock(return_value="/tmp/deploy-repo")
    manifests.commit_and_push_changes = AsyncMock(return_value=True)
    pm.get_git_connector_for_deployment = AsyncMock(return_value=manifests)

    deployment = _deployment()
    pm.get_contents = AsyncMock(
        return_value={
            "name": "test-project",
            "services": [],
            "deployments": [deployment],
            "repositories": [{"name": "main-repo", "path": ""}],
        }
    )
    pm.get_deployment_by_name = AsyncMock(return_value=deployment)
    pm.mutate_and_commit_project = AsyncMock(return_value=True)

    return DeleteProjectManager(pm), gitops, manifests


def _niets_bestaat(path: str) -> bool:
    return False


def _alleen_de_manifestmap(path: str) -> bool:
    """De manifestmap in de deployments-repo staat er, de bestanden in de argo-repo niet."""
    return str(path).startswith("/tmp/deploy-repo")


async def _delete(manager: DeleteProjectManager, exists=_niets_bestaat) -> dict[str, Any]:
    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=False)

    store = MagicMock()
    store.get.return_value = MagicMock(filename="test-project.yaml")

    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)

    with (
        patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
        patch("opi.manager.delete_project_manager.get_project_store", return_value=store),
        patch("os.path.exists", side_effect=exists),
        patch("shutil.rmtree", MagicMock()),
        patch("opi.manager.delete_project_manager.SubdomainConnector", subdomain),
    ):
        return await manager.delete_deployment("test-project", "pr-42")


def _project_file_ops(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [op for op in result["operations"] if op["type"] == "project_file_update"]


async def test_een_mislukte_push_van_stap_2_laat_de_deployment_staan() -> None:
    """De fout van stap 2 gaat DOOR (RC-265). Voorheen werd hij afgevangen en liep de
    delete toch naar stap 4, waar hij 30 minuten wachtte tot ArgoCD een Application
    opruimde die nog gewoon in git stond. Een doorgegooide fout krijgt de herhaling van de
    worker; een fout die de handler als resultaat meldt krijgt die niet."""
    manager, gitops, _ = _delete_manager()
    gitops.mutate_and_push = AsyncMock(side_effect=GitPushConflictError("Remote has conflicting changes"))

    with pytest.raises(HTTPException) as fout:
        await _delete(manager)

    assert "conflicting changes" in str(fout.value.detail)
    # 5xx en niet 4xx: de worker gooit een 5xx door naar zijn algemene tak, die de taak op
    # zijn eigen max_attempts opnieuw aanbiedt. Een 4xx zou hij als blijvend afdoen.
    assert fout.value.status_code >= 500, fout.value.status_code
    manager.project_manager.mutate_and_commit_project.assert_not_awaited()


async def test_een_mislukte_commit_in_stap_2_wacht_niet_op_argocd() -> None:
    """Stap 1 leest alleen, dus er is dan nog niets weggegooid. De taak hoort binnen
    seconden te eindigen zonder de wachtlus, de namespace, de database of de bucket aan te
    raken: de drie taken van 15 oktober liepen zo tot de maximale taakduur van 1800s."""
    manager, gitops, _ = _delete_manager()
    gitops.mutate_and_push = AsyncMock(side_effect=GitPushConflictError("Remote has moved"))

    argo = AsyncMock()
    argo.application_exists = AsyncMock(return_value=True)
    store = MagicMock()
    store.get.return_value = MagicMock(filename="test-project.yaml")
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)

    begonnen = time.monotonic()
    with (
        patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
        patch("opi.manager.delete_project_manager.get_project_store", return_value=store),
        patch("os.path.exists", side_effect=_niets_bestaat),
        patch("shutil.rmtree", MagicMock()),
        patch("opi.manager.delete_project_manager.SubdomainConnector", subdomain),
        pytest.raises(HTTPException),
    ):
        await manager.delete_deployment("test-project", "pr-42")

    assert time.monotonic() - begonnen < 1, "de delete heeft ergens op gewacht"
    argo.wait_for_application_deletion.assert_not_awaited()
    pm = manager.project_manager
    pm._kubectl_connector.delete_namespace.assert_not_awaited()
    pm._keycloak_manager.delete_resources_for_deployment.assert_not_awaited()
    pm._minio_manager.delete_resources_for_deployment.assert_not_awaited()
    pm._ensure_database_manager.return_value.delete_resources_for_deployment.assert_not_awaited()
    pm.mutate_and_commit_project.assert_not_awaited()


async def test_een_mislukte_commit_van_de_manifestmap_laat_de_deployment_staan() -> None:
    manager, _gitops, manifests = _delete_manager()
    manifests.commit_and_push_changes = AsyncMock(return_value=False)

    result = await _delete(manager, exists=_alleen_de_manifestmap)

    manager.project_manager.mutate_and_commit_project.assert_not_awaited()
    assert result["success"] is False
    assert [op["status"] for op in _project_file_ops(result)] == ["skipped"]


async def test_een_fout_bij_het_weghalen_van_de_manifestmap_laat_de_deployment_staan() -> None:
    manager, _gitops, manifests = _delete_manager()
    manifests.commit_and_push_changes = AsyncMock(side_effect=RuntimeError("git push weigerde"))

    result = await _delete(manager, exists=_alleen_de_manifestmap)

    manager.project_manager.mutate_and_commit_project.assert_not_awaited()
    assert result["success"] is False
    assert [op["status"] for op in _project_file_ops(result)] == ["skipped"]


async def test_een_delete_die_slaagt_haalt_de_deployment_nog_steeds_weg() -> None:
    manager, _gitops, _ = _delete_manager()

    result = await _delete(manager)

    manager.project_manager.mutate_and_commit_project.assert_awaited_once()
    assert result["success"] is True, result["errors"]
    assert [op["status"] for op in _project_file_ops(result)] == ["success"]


async def test_de_appproject_en_repo_bestanden_zijn_weg_voor_de_kustomization_herbouw(tmp_path: Path) -> None:
    """De AppProject en de repository-secretbestanden stonden NA de commit van stap 2,
    dus hun verwijdering bleef ongecommit in de werkboom staan en de kustomization die er
    net voor herbouwd was noemde ze nog. Ze gaan sinds RC-265 mee in dezelfde mutatie, voor
    de herbouw: deze toets bewijst dat op een echte werkmap, niet alleen op een gestubde."""
    manager, gitops, manifests = _delete_manager()

    werkmap = tmp_path / "argo-applications"
    project_dir = werkmap / "local" / "test-project"
    project_dir.mkdir(parents=True)
    gitops.get_working_dir = AsyncMock(return_value=str(werkmap))
    manifests.get_working_dir = AsyncMock(return_value=str(tmp_path / "deploy-repo"))

    argocd_app_path = werkmap / generate_gitops_argocd_application_path("local", "test-project", "pr-42")
    appproject_name = generate_argocd_appproject_prefix("test-project", "test-project")
    appproject_path = project_dir / get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
    repo_paths = [
        project_dir / get_output_filename_from_template(template, "test-project-main-repo")
        for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]
    ]
    for pad in [argocd_app_path, appproject_path, *repo_paths]:
        pad.write_text("kind: Placeholder\n")

    nog_aanwezig_bij_herbouw: list[bool] = []

    def _herbouw(output_dir: str, **kwargs: Any) -> bool:
        nog_aanwezig_bij_herbouw.append(appproject_path.exists() or any(p.exists() for p in repo_paths))
        return True

    manager.project_manager._manifest_generator.create_kustomization_files = MagicMock(side_effect=_herbouw)

    result = await _delete(manager, exists=os.path.exists)

    assert nog_aanwezig_bij_herbouw == [False], (
        "de AppProject en/of de repository-bestanden stonden nog toen de kustomization herbouwd werd"
    )
    assert not appproject_path.exists()
    assert not any(p.exists() for p in repo_paths)
    assert result["success"] is True, result["errors"]
