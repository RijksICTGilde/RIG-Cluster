"""Een verwijdering geeft een uitkomst die klopt (RC-385).

De vier reproducties van RC-379 die hierbij horen staan in
``test_verwijderpaden_reproducties.py``. Dit bestand meet de keerzijde van elke reparatie:
de grendel die WEL moet tegenhouden, de volgorde waarin het AppProject vertrekt, het
verschil tussen een Forbidden en een timeout, en de hermeting die de uitkomst bepaalt.
Achtergrond: ``features/futures/verwijderpaden-in-kaart.md``, sectie 6.1 en 6.4.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_argo_namespace
from opi.manager.delete_project_manager import DeleteProjectManager, ForcedDeletion
from opi.utils.naming import (
    generate_argocd_appproject_prefix,
    generate_argocd_project_application_name,
    get_output_filename_from_template,
)
from tests.test_delete_project_manager import _make_deployment, _make_project_data, _make_project_manager_mock
from tests.test_verwijderpaden_reproducties import (
    _project_manager_met_levend_projectbestand,
    _twee_deployments_in_een_namespace,
)
from tests.test_zombie_deployment import _delete_manager, _deployment, _niets_bestaat

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# De namespace-opruiming na de lus over de deployments (pad 1, plan-item 1)
# ---------------------------------------------------------------------------


async def test_een_al_afwezige_namespace_wordt_niet_nog_eens_verwijderd() -> None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=True)
    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}

    await manager._delete_project_namespaces("test-project", "local", [{"namespace": "test-project"}], resultaat)

    pm._kubectl_connector.delete_namespace.assert_not_awaited()
    assert resultaat["operations"] == [
        {"type": "project_namespace_deletion", "target": "rig-test-project", "status": "not_found"}
    ]
    assert resultaat["success"] is True


async def test_een_mislukte_delete_meldt_een_fout_en_zet_mislukt() -> None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=False)
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=False)
    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}

    await manager._delete_project_namespaces("test-project", "local", [{"namespace": "test-project"}], resultaat)

    pm._kubectl_connector.wait_for_namespace_deletion.assert_not_awaited()
    assert resultaat["operations"] == [
        {"type": "project_namespace_deletion", "target": "rig-test-project", "status": "error"}
    ]
    assert resultaat["success"] is False
    assert any("rig-test-project" in fout for fout in resultaat["errors"])


async def test_een_timeout_op_de_wacht_meldt_timeout_en_een_fout() -> None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=False)
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=True)
    pm._kubectl_connector.wait_for_namespace_deletion = AsyncMock(return_value=False)
    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}

    await manager._delete_project_namespaces("test-project", "local", [{"namespace": "test-project"}], resultaat)

    assert resultaat["operations"] == [
        {"type": "project_namespace_deletion", "target": "rig-test-project", "status": "timeout"}
    ]
    assert resultaat["success"] is False
    assert any("not confirmed gone" in fout for fout in resultaat["errors"])


async def test_een_geslaagde_opruiming_meldt_success_zonder_fout() -> None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=False)
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=True)
    pm._kubectl_connector.wait_for_namespace_deletion = AsyncMock(return_value=True)
    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}

    await manager._delete_project_namespaces("test-project", "local", [{"namespace": "test-project"}], resultaat)

    assert resultaat["operations"] == [
        {"type": "project_namespace_deletion", "target": "rig-test-project", "status": "success"}
    ]
    assert resultaat["errors"] == []
    assert resultaat["success"] is True


async def test_twee_deployments_in_een_namespace_verwijderen_hem_maar_een_keer() -> None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=False)
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=True)
    pm._kubectl_connector.wait_for_namespace_deletion = AsyncMock(return_value=True)
    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}

    await manager._delete_project_namespaces(
        "test-project",
        "local",
        [{"namespace": "gedeeld"}, {"namespace": "gedeeld"}],
        resultaat,
    )

    assert pm._kubectl_connector.delete_namespace.await_count == 1
    assert len(resultaat["operations"]) == 1


# ---------------------------------------------------------------------------
# De grendel op de namespace: een deployment die BLIJFT houdt hem tegen
# ---------------------------------------------------------------------------


async def test_een_blijvende_deployment_houdt_de_namespace_tegen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zonder ``leaving`` is de zuster een blijver, en dan blijft de namespace staan."""
    bestand: dict[str, Any] = {
        "name": "test-project",
        "services": [],
        "deployments": _twee_deployments_in_een_namespace(),
        "repositories": [{"name": "main-repo", "path": ""}],
    }
    pm = _project_manager_met_levend_projectbestand(bestand)
    pm._project_file_handler.deployment_uses_service = MagicMock(return_value=False)
    manager = DeleteProjectManager(pm)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=False)
    monkeypatch.setattr("opi.manager.delete_project_manager.create_argo_connector", lambda: argo)

    with patch("os.path.exists", side_effect=_niets_bestaat), patch("shutil.rmtree", MagicMock()):
        resultaat = await manager.delete_deployment("test-project", "productie")

    pm._kubectl_connector.delete_namespace.assert_not_awaited()
    overgeslagen = [
        op for op in resultaat["operations"] if op["type"] == "namespace_deletion" and op["status"] == "skipped"
    ]
    assert overgeslagen
    assert overgeslagen[0]["reason"] == "Namespace still used by other deployments"


# ---------------------------------------------------------------------------
# De volgorde: het AppProject verlaat de map na de wacht, in een eigen commit
# ---------------------------------------------------------------------------


def _argo_projectmap(werkmap: Path, project: str, namespace: str) -> Path:
    """De argo-projectmap zoals pad 1 hem aantreft: de projectapplicatie en het AppProject."""
    project_dir = werkmap / "local" / project
    project_dir.mkdir(parents=True)
    appproject_name = generate_argocd_appproject_prefix(project, namespace)
    (project_dir / get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)).write_text(
        yaml.safe_dump({"kind": "AppProject", "metadata": {"name": appproject_name}})
    )
    project_app = generate_argocd_project_application_name(project)
    (project_dir / get_output_filename_from_template("argocd-application.yaml.jinja", project_app)).write_text(
        yaml.safe_dump({"kind": "Application", "metadata": {"name": project_app}, "spec": {"project": appproject_name}})
    )
    return project_dir


def _verloop_van_een_projectverwijdering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, applicatie_weg: bool
) -> tuple[DeleteProjectManager, list[tuple[str, str]], Path]:
    """Een ``delete_project`` met een echte argo-projectmap, en een logboek van de stappen."""
    bestand: dict[str, Any] = {
        "name": "test-project",
        "services": [],
        "deployments": [_twee_deployments_in_een_namespace()[0]],
        "repositories": [{"name": "main-repo", "path": ""}],
    }
    pm = _project_manager_met_levend_projectbestand(bestand)
    manager = DeleteProjectManager(pm)

    werkmap = tmp_path / "argo-applications"
    project_dir = _argo_projectmap(werkmap, "test-project", "test-project")
    verloop: list[tuple[str, str]] = []

    appproject_file = get_output_filename_from_template(
        "argocd-appproject.yaml.jinja", generate_argocd_appproject_prefix("test-project", "test-project")
    )

    gitops = AsyncMock()
    gitops.get_working_dir = AsyncMock(return_value=str(werkmap))

    async def _mutate_and_push(message: str, mutate: Any) -> Any:
        uitkomst = await mutate()
        verloop.append(("commit", "appproject-staat-er" if (project_dir / appproject_file).exists() else "map-leeg"))
        return uitkomst

    gitops.mutate_and_push = AsyncMock(side_effect=_mutate_and_push)
    pm.get_git_connector_for_argocd = AsyncMock(return_value=gitops)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)

    async def _wait(app_name: str, **kwargs: Any) -> bool:
        verloop.append(("wacht", app_name))
        return applicatie_weg

    argo.wait_for_application_deletion = AsyncMock(side_effect=_wait)
    monkeypatch.setattr("opi.manager.delete_project_manager.create_argo_connector", lambda: argo)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_project = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    monkeypatch.setattr(
        manager, "delete_deployment", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )
    for naam in ["_cleanup_project_infrastructure", "_cleanup_project_keycloak_realm", "_delete_project_level_folder"]:
        monkeypatch.setattr(manager, naam, AsyncMock())
    monkeypatch.setattr(manager, "_delete_project_namespaces", AsyncMock())
    monkeypatch.setattr(
        manager, "_delete_project_file", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )
    return manager, verloop, project_dir


async def test_het_appproject_gaat_pas_na_de_wacht_uit_de_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, verloop, project_dir = _verloop_van_een_projectverwijdering(tmp_path, monkeypatch, applicatie_weg=True)

    await manager.delete_project("test-project")

    assert verloop == [
        ("commit", "appproject-staat-er"),
        ("wacht", generate_argocd_project_application_name("test-project")),
        ("commit", "map-leeg"),
    ], verloop
    assert not project_dir.exists()


async def test_een_staande_applicatie_houdt_het_appproject_in_de_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, verloop, project_dir = _verloop_van_een_projectverwijdering(tmp_path, monkeypatch, applicatie_weg=False)

    resultaat = await manager.delete_project("test-project")

    assert [stap for stap, _ in verloop] == ["commit", "wacht"], verloop
    assert project_dir.exists()
    assert resultaat["success"] is False
    assert any("stays in the GitOps repository" in fout for fout in resultaat["errors"])


async def test_een_onleesbaar_applicatiebestand_houdt_het_appproject_in_de_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Onbekend is hier geen vrijbrief: een bestand dat niet te lezen is kan net zo goed
    een Application zijn die naar dit AppProject wijst, dus telt het mee als standing."""
    manager, gitops, _manifests = _delete_manager()
    werkmap = tmp_path / "argo-applications"
    project_dir = werkmap / "local" / "test-project"
    project_dir.mkdir(parents=True)
    (project_dir / "broken.yaml").write_text("key: [unclosed\n")

    gitops.get_working_dir = AsyncMock(return_value=str(werkmap))

    async def _mutate_and_push(message: str, mutate: Any) -> Any:
        return await mutate()

    gitops.mutate_and_push = AsyncMock(side_effect=_mutate_and_push)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    monkeypatch.setattr("opi.manager.delete_project_manager.create_argo_connector", lambda: argo)

    resultaat: dict[str, Any] = {"operations": [], "errors": [], "success": True}
    verder = await manager._release_project_applications("test-project", "local", resultaat)

    assert verder is False
    assert any("stays in the GitOps repository" in fout for fout in resultaat["errors"]), resultaat["errors"]
    assert resultaat["success"] is False
    bestanden = [op for op in resultaat["operations"] if op["type"] == "project_application_file_deletion"]
    assert bestanden
    assert bestanden[0]["unreadable"] == ["broken.yaml"]
    wacht = [op for op in resultaat["operations"] if op["type"] == "project_application_deletion_wait"]
    assert wacht
    assert wacht[0]["standing"] == ["broken.yaml"]
    argo.wait_for_application_deletion.assert_not_awaited()


# ---------------------------------------------------------------------------
# De maatstaf van de inventaris: Forbidden op een vreemd type is geen gat
# ---------------------------------------------------------------------------

_EEN_SECRET = {
    "items": [
        {
            "kind": "Secret",
            "metadata": {
                "name": "web",
                "namespace": "rig-prd-nd-j7s",
                "labels": {"app.kubernetes.io/instance": "nd-j7s-pr-61"},
            },
        }
    ]
}


async def _inventariseer(stderr: str, own_types: list[str] | None = None) -> tuple[list[Any], bool] | None:
    connector = KubectlConnector()
    with patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(json.dumps(_EEN_SECRET), stderr, 1))):
        return await connector.list_tracked_resources(
            "rig-prd-nd-j7s", ["secrets", "podtemplates"], own_types=own_types
        )


async def test_een_forbidden_op_een_vreemd_type_laat_de_inventaris_compleet() -> None:
    uitkomst = await _inventariseer(
        'Error from server (Forbidden): podtemplates is forbidden: User "x" cannot list resource "podtemplates"',
        own_types=["secrets"],
    )

    assert uitkomst is not None
    resources, complete = uitkomst
    assert complete is True, "een type dat ZAD niet rendert kan niets van ons bevatten"
    assert len(resources) == 1


async def test_een_timeout_op_een_eigen_type_maakt_de_inventaris_onvolledig() -> None:
    uitkomst = await _inventariseer(
        "Error from server (ServiceUnavailable): the server is currently unable to handle the request (get secrets)",
        own_types=["secrets"],
    )

    assert uitkomst is not None
    assert uitkomst[1] is False, "een tijdelijke fout kan een type van ons stil houden"


async def test_een_smalle_repo_maakt_de_inventaris_niet_smaller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De manifest-repo van de tenant mag de vraag aan het cluster niet verkleinen.

    Hij zegt alleen welke Forbidden geen gat is: anders laat een repo die een kind niet
    meer noemt de veegactie schoon melden terwijl de resource in de namespace blijft.
    """
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)
    manifest = tmp_path / "local" / "test-project" / "pr-1"
    manifest.mkdir(parents=True)
    (manifest / "configmap.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")

    kubectl = AsyncMock()
    kubectl.get_argocd_application_destination_namespace = AsyncMock(return_value="rig-test-project")
    kubectl.argocd_application_resource_types = AsyncMock(return_value=None)
    kubectl.list_namespaced_kinds = AsyncMock(return_value={"ConfigMap": "configmaps", "Secret": "secrets"})
    kubectl.list_tracked_resources = AsyncMock(return_value=([], True))
    kubectl.delete_tracked_resources = AsyncMock(return_value=[])
    kubectl.list_argocd_applications = AsyncMock(return_value=[{"metadata": {"name": "test-project-pr-1"}}])
    kubectl.remove_argocd_application_finalizers = AsyncMock(return_value=True)

    pm = AsyncMock()
    pm._kubectl_connector = kubectl
    manager = DeleteProjectManager(pm)

    await manager._force_delete_stuck_application("test-project-pr-1", {"operations": [], "errors": []}, str(tmp_path))

    gevraagd, eigen = kubectl.list_tracked_resources.await_args.args[1:3]
    assert gevraagd is None, f"de repo bepaalde welke typen het cluster gevraagd werd: {gevraagd}"
    assert eigen == ["configmaps"], eigen


# ---------------------------------------------------------------------------
# De werkmap van de deployments-repo volgt repository.path
# ---------------------------------------------------------------------------


async def _werkmap(repo_path: str) -> str | None:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    connector = AsyncMock()
    connector.ensure_repo_cloned = AsyncMock(return_value=None)
    connector.get_working_dir = AsyncMock(return_value="/tmp/deploy-repo")
    pm.get_git_connector_for_deployment = AsyncMock(return_value=connector)

    project_data = {"repositories": [{"name": "main-repo", "path": repo_path}]}
    return await manager._deployments_repo_dir(project_data, "main-repo")


async def test_een_submap_in_de_repo_komt_in_de_werkmap_terug() -> None:
    """De manifesten van een deployment kunnen in een submap van de repo staan (zoals
    het sandboxproject ``e2e76-7x2``): zonder die submap mist ``rendered_kinds`` elk
    kind dat erin staat, en dan zegt de veegactie 'compleet' over een lege meting."""
    assert await _werkmap("infra") == os.path.join("/tmp/deploy-repo", "infra")


async def test_een_lege_repo_path_laat_de_werkmap_ongewijzigd() -> None:
    assert await _werkmap("") == "/tmp/deploy-repo"


async def test_een_punt_als_repo_path_laat_de_werkmap_ongewijzigd() -> None:
    assert await _werkmap(".") == "/tmp/deploy-repo"


async def test_een_repo_path_buiten_de_kloon_levert_geen_werkmap() -> None:
    """Een bestaand projectbestand komt niet opnieuw langs ``REPOSITORY_PATH_PATTERN``
    (features/repository-pad.md), dus een pad met ``..`` moet hier dicht vallen in plaats
    van de inventaris over een map buiten de kloon te laten oordelen."""
    assert await _werkmap("a/../../../etc") is None


# ---------------------------------------------------------------------------
# De hermeting bepaalt de uitkomst, niet de foutenlijst van de rit
# ---------------------------------------------------------------------------


async def _verwijder_deployment(
    kubectl_antwoorden: dict[str, Any], fout_tijdens_de_rit: bool
) -> tuple[dict[str, Any], AsyncMock]:
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=kubectl_antwoorden["application_exists"])
    pm._kubectl_connector.resource_exists = AsyncMock(return_value=kubectl_antwoorden["appproject_exists"])
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=kubectl_antwoorden["namespace_absent"])
    # Wie bij de hermeting nog staat, stond ook na de wacht nog; de wacht zelf haalt
    # alles weg wat bij de meting weg is.
    pm._kubectl_connector.wait_for_resource_absence = AsyncMock(
        return_value=kubectl_antwoorden["appproject_exists"] is not True
    )
    if fout_tijdens_de_rit:
        pm._keycloak_manager.delete_resources_for_deployment = AsyncMock(
            return_value={"operations": [], "errors": ["Keycloak gaf 503 tijdens de rit"]}
        )

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
        patch("os.path.exists", side_effect=_niets_bestaat),
        patch("shutil.rmtree", MagicMock()),
        patch("opi.manager.delete_project_manager.SubdomainConnector", subdomain),
    ):
        return await manager.delete_deployment("test-project", "pr-42"), pm


async def test_een_fout_tijdens_de_rit_met_een_schone_eindtoestand_is_geslaagd() -> None:
    resultaat, _pm = await _verwijder_deployment(
        {"application_exists": False, "appproject_exists": False, "namespace_absent": True},
        fout_tijdens_de_rit=True,
    )

    assert resultaat["success"] is True, resultaat["errors"]
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "clean"
    assert hermeting[0]["explained_errors"], "de fouten van de rit blijven als uitleg in het resultaat staan"


async def test_een_namespace_die_blijft_staan_is_niet_geslaagd() -> None:
    resultaat, _pm = await _verwijder_deployment(
        {"application_exists": False, "appproject_exists": False, "namespace_absent": False},
        fout_tijdens_de_rit=False,
    )

    assert resultaat["success"] is False
    assert any("namespace" in fout and "still stands" in fout for fout in resultaat["errors"]), resultaat["errors"]
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "incomplete"


async def test_een_onleesbare_hermeting_laat_de_foutenlijst_beslissen() -> None:
    resultaat, _pm = await _verwijder_deployment(
        {"application_exists": None, "appproject_exists": None, "namespace_absent": None},
        fout_tijdens_de_rit=False,
    )

    assert resultaat["success"] is True
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "unreadable"


async def test_de_hermeting_van_een_deployment_wacht_eerst_op_het_appproject() -> None:
    """Het AppProject ging deze ronde uit git; ArgoCD prunt asynchroon (RC-389)."""
    resultaat, pm = await _verwijder_deployment(
        {"application_exists": False, "appproject_exists": False, "namespace_absent": True},
        fout_tijdens_de_rit=False,
    )

    assert resultaat["success"] is True, resultaat["errors"]
    pm._kubectl_connector.wait_for_resource_absence.assert_awaited_once_with(
        "appproject",
        generate_argocd_appproject_prefix("test-project", "test-project"),
        get_argo_namespace("local"),
    )


async def test_een_appproject_dat_bij_een_deployment_blijft_staan_is_niet_geslaagd() -> None:
    """Ook na de wacht: de hermeting beslist zoals hij vandaag beslist."""
    resultaat, _pm = await _verwijder_deployment(
        {"application_exists": False, "appproject_exists": True, "namespace_absent": True},
        fout_tijdens_de_rit=False,
    )

    assert resultaat["success"] is False
    assert any("AppProject" in fout and "still stands" in fout for fout in resultaat["errors"]), resultaat["errors"]
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Laag 1 van de hermeting: een met opzet bewaarde entry is geen geslaagde delete,
# ook niet als de rest van het cluster (al dan niet via een race) schoon meet.
# ---------------------------------------------------------------------------


async def test_een_onbevestigde_app_houdt_de_entry_ook_als_de_hermeting_schoon_is() -> None:
    """Geval 5: force verwijdert de namespace alvast, maar de Application was niet
    bevestigd weg. Een hermeting die het cluster daarna toch schoon aantreft (de race
    waar de hermeting zelf voor bestaat) mag de entry niet alsnog als verwijderd melden."""
    manager, _gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.resource_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=True)
    manager._force_delete_stuck_application = AsyncMock(
        return_value=ForcedDeletion(finalizer_removed=False, swept_partially=False)
    )

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=True)
    argo.wait_for_application_deletion = AsyncMock(return_value=False)
    store = MagicMock()
    store.get.return_value = MagicMock(filename="test-project.yaml")
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)

    with (
        patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
        patch("opi.manager.delete_project_manager.get_project_store", return_value=store),
        patch("os.path.exists", side_effect=_niets_bestaat),
        patch("shutil.rmtree", MagicMock()),
        patch("opi.manager.delete_project_manager.SubdomainConnector", subdomain),
    ):
        resultaat = await manager.delete_deployment("test-project", "pr-42", force=True)

    assert resultaat["success"] is False, resultaat
    assert any("not confirmed deleted" in fout for fout in resultaat["errors"]), resultaat["errors"]
    manager.project_manager.mutate_and_commit_project.assert_not_awaited()
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "incomplete"
    assert hermeting[0]["project_file_settled"] is False


async def test_een_zuster_die_deze_ronde_weggaat_houdt_de_namespace_niet_tegen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De keerzijde van de grendel: in ``leaving`` is de zuster geen blijver (geval 5)."""
    bestand: dict[str, Any] = {
        "name": "test-project",
        "services": [],
        "deployments": _twee_deployments_in_een_namespace(),
        "repositories": [{"name": "main-repo", "path": ""}],
    }
    pm = _project_manager_met_levend_projectbestand(bestand)
    pm._project_file_handler.deployment_uses_service = MagicMock(return_value=False)
    manager = DeleteProjectManager(pm)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=False)
    monkeypatch.setattr("opi.manager.delete_project_manager.create_argo_connector", lambda: argo)

    with patch("os.path.exists", side_effect=_niets_bestaat), patch("shutil.rmtree", MagicMock()):
        resultaat = await manager.delete_deployment("test-project", "productie", leaving={"productie", "acceptatie"})

    pm._kubectl_connector.delete_namespace.assert_awaited_once_with("rig-test-project")
    assert not [
        op for op in resultaat["operations"] if op["type"] == "namespace_deletion" and op["status"] == "skipped"
    ], resultaat["operations"]


# ---------------------------------------------------------------------------
# De hermeting van pad 1 beslist over het projectbestand (plan-item 4 en 5)
# ---------------------------------------------------------------------------


async def _verwijder_project(
    namespace_blijft: bool,
    monkeypatch: pytest.MonkeyPatch,
    appproject_blijft: bool = False,
    map_uit_git: bool = True,
) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
    """Een geslaagde deploymentronde, waarna alleen de hermeting nog iets te zeggen heeft."""
    deployment = _make_deployment()
    pm = _make_project_manager_mock()
    pm.get_contents = AsyncMock(return_value=_make_project_data(deployments=[deployment]))
    pm.get_deployments = AsyncMock(return_value=[deployment])
    pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.resource_exists = AsyncMock(return_value=appproject_blijft)
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=not namespace_blijft)
    pm._kubectl_connector.wait_for_resource_absence = AsyncMock(return_value=not appproject_blijft)
    manager = DeleteProjectManager(pm)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_project = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    monkeypatch.setattr(
        manager, "delete_deployment", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )
    for naam in (
        "_cleanup_project_infrastructure",
        "_cleanup_project_keycloak_realm",
        "_delete_project_level_folder",
        "_release_project_applications",
        "_delete_project_argocd_folder",
        "_delete_project_namespaces",
    ):
        monkeypatch.setattr(manager, naam, AsyncMock(return_value=True))
    if not map_uit_git:
        # De map ging deze ronde niet uit git (bijvoorbeeld not_found), dus er valt
        # niets af te wachten.
        monkeypatch.setattr(manager, "_delete_project_argocd_folder", AsyncMock(return_value=False))
    projectbestand_weg = AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    monkeypatch.setattr(manager, "_delete_project_file", projectbestand_weg)

    return await manager.delete_project("test-project"), projectbestand_weg, pm


async def test_een_schone_hermeting_laat_het_projectbestand_gaan(monkeypatch: pytest.MonkeyPatch) -> None:
    resultaat, projectbestand_weg, _pm = await _verwijder_project(namespace_blijft=False, monkeypatch=monkeypatch)

    assert resultaat["success"] is True, resultaat["errors"]
    projectbestand_weg.assert_awaited_once()
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "clean"


async def test_een_namespace_die_blijft_houdt_het_projectbestand_ook_zonder_fout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elke stap meldde zich geslaagd, dus alleen de hermeting kan dit nog tegenhouden."""
    resultaat, projectbestand_weg, _pm = await _verwijder_project(namespace_blijft=True, monkeypatch=monkeypatch)

    assert resultaat["success"] is False, resultaat
    projectbestand_weg.assert_not_awaited()
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting
    assert hermeting[0]["status"] == "incomplete"
    assert any("namespace" in rest for rest in hermeting[0]["remaining"]), hermeting[0]


# ---------------------------------------------------------------------------
# De AppProject-wacht vóór de hermeting (RC-389): ArgoCD prunt asynchroon
# ---------------------------------------------------------------------------


async def test_een_appproject_dat_tijdens_de_wacht_verdwijnt_geeft_een_schone_rit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De valse mislukking uit het incident: de map is uit git, de prune loopt na."""
    resultaat, projectbestand_weg, pm = await _verwijder_project(
        namespace_blijft=False, monkeypatch=monkeypatch, appproject_blijft=False
    )

    assert resultaat["success"] is True, resultaat["errors"]
    pm._kubectl_connector.wait_for_resource_absence.assert_awaited_once_with(
        "appproject",
        generate_argocd_appproject_prefix("test-project", "test-project"),
        get_argo_namespace("local"),
    )
    projectbestand_weg.assert_awaited_once()
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting[0]["status"] == "clean"


async def test_een_appproject_dat_blijft_staan_houdt_het_projectbestand_na_de_wacht(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De wacht verandert de beslissing niet: wie na de timeout nog staat, staat er."""
    resultaat, projectbestand_weg, pm = await _verwijder_project(
        namespace_blijft=False, monkeypatch=monkeypatch, appproject_blijft=True
    )

    assert resultaat["success"] is False, resultaat
    assert pm._kubectl_connector.wait_for_resource_absence.await_count == 1
    projectbestand_weg.assert_not_awaited()
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting[0]["status"] == "incomplete"
    assert any("AppProject" in rest for rest in hermeting[0]["remaining"]), hermeting[0]


async def test_zonder_map_die_uit_git_ging_is_er_niets_om_op_te_wachten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Een delete die vóór stap 4.7 faalde wacht niet 40s op een AppProject dat nooit weggehaald is."""
    resultaat, _projectbestand_weg, pm = await _verwijder_project(
        namespace_blijft=False, monkeypatch=monkeypatch, map_uit_git=False
    )

    assert resultaat["success"] is True, resultaat["errors"]
    pm._kubectl_connector.wait_for_resource_absence.assert_not_awaited()


# ---------------------------------------------------------------------------
# De delete meldt zijn stappen (RC-389): per deployment met onderwerp
# ---------------------------------------------------------------------------


def _stappen_van(progress: MagicMock) -> list[tuple[str, str | None]]:
    return [(call.args[0], call.kwargs.get("subject")) for call in progress.add_task.call_args_list]


def _delete_deployment_met_stappen(
    extra_deployments: list[dict[str, Any]] | None = None,
    push_fout: Exception | None = None,
    namespace_delete: bool = True,
) -> tuple[Callable[[], Awaitable[dict[str, Any]]], MagicMock, AsyncMock]:
    """Een delete_deployment-ronde met een voortgangsmanager: geeft (run, progress, pm)."""
    manager, gitops, _manifests = _delete_manager()
    pm = manager.project_manager
    pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.resource_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=True)
    pm._kubectl_connector.wait_for_resource_absence = AsyncMock(return_value=True)
    pm._kubectl_connector.delete_namespace = AsyncMock(return_value=namespace_delete)
    if extra_deployments:
        data = dict(pm.get_contents.return_value)
        data["deployments"] = [*data["deployments"], *extra_deployments]
        pm.get_contents = AsyncMock(return_value=data)
    if push_fout is not None:
        gitops.mutate_and_push = AsyncMock(side_effect=push_fout)

    progress = MagicMock()
    progress.add_task = MagicMock(side_effect=lambda name, subject=None: f"stap-{name}")
    pm.get_progress_manager = MagicMock(return_value=progress)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=False)
    store = MagicMock()
    store.get.return_value = MagicMock(filename="test-project.yaml")
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)

    async def _run() -> dict[str, Any]:
        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=store),
            patch("os.path.exists", side_effect=_niets_bestaat),
            patch("shutil.rmtree", MagicMock()),
            patch("opi.manager.delete_project_manager.SubdomainConnector", subdomain),
        ):
            return await manager.delete_deployment("test-project", "pr-42")

    return _run, progress, pm


async def test_een_deployment_verwijderen_meldt_elke_stap_met_de_deployment_als_onderwerp() -> None:
    run, progress, _pm = _delete_deployment_met_stappen()

    resultaat = await run()

    assert resultaat["success"] is True, resultaat["errors"]
    assert _stappen_van(progress) == [
        ("ArgoCD-toepassing verwijderen", "pr-42"),
        ("Namespace verwijderen", "pr-42"),
        ("Services opruimen", "pr-42"),
        ("Manifesten verwijderen", "pr-42"),
        ("Deployment uit het projectbestand verwijderen", "pr-42"),
        ("Subdomein opruimen", "pr-42"),
        ("Controleren dat alles verwijderd is", "pr-42"),
    ]
    # Elke stap werd ook afgesloten.
    assert progress.complete_task.call_count == 7


async def test_een_stap_valt_weg_als_zijn_werk_niet_draait() -> None:
    """De namespace deelt `pr-42` met `acceptatie`; die blijft, dus de namespace blijft
    ook en er mag geen stap 'Namespace verwijderen' groen kleuren."""
    zuster = {**_deployment(), "name": "acceptatie"}
    run, progress, pm = _delete_deployment_met_stappen(extra_deployments=[zuster])

    resultaat = await run()

    assert resultaat["success"] is True, resultaat["errors"]
    pm._kubectl_connector.delete_namespace.assert_not_awaited()
    assert _stappen_van(progress) == [
        ("ArgoCD-toepassing verwijderen", "pr-42"),
        ("Services opruimen", "pr-42"),
        ("Manifesten verwijderen", "pr-42"),
        ("Deployment uit het projectbestand verwijderen", "pr-42"),
        ("Subdomein opruimen", "pr-42"),
        ("Controleren dat alles verwijderd is", "pr-42"),
    ]


async def test_een_stap_die_omvalt_wordt_als_mislukt_gemeld() -> None:
    """De stap kleurt niet groen terwijl het werk eronder knapt."""
    run, progress, _pm = _delete_deployment_met_stappen(push_fout=RuntimeError("git is stuk"))

    with pytest.raises(HTTPException, match="git is stuk"):
        await run()

    progress.fail_task.assert_called_once_with("stap-ArgoCD-toepassing verwijderen", "git is stuk")
    progress.complete_task.assert_not_called()


async def test_een_stap_met_nieuwe_fouten_sluit_als_mislukt() -> None:
    """Fouten die tijdens de stap bijkwamen zijn zijn sluitreden. De rit zelf loopt door;
    de hermeting aan het eind (namespace uiteindelijk weg) beslist over de uitkomst."""
    run, progress, _pm = _delete_deployment_met_stappen(namespace_delete=False)

    resultaat = await run()

    stap, fout = progress.fail_task.call_args.args
    assert stap == "stap-Namespace verwijderen"
    assert "rig-test-project" in fout
    assert [naam for naam, _ in _stappen_van(progress)] == [
        "ArgoCD-toepassing verwijderen",
        "Namespace verwijderen",
        "Services opruimen",
        "Manifesten verwijderen",
        "Deployment uit het projectbestand verwijderen",
        "Subdomein opruimen",
        "Controleren dat alles verwijderd is",
    ]
    assert resultaat["success"] is True, resultaat["errors"]
    hermeting = [op for op in resultaat["operations"] if op["type"] == "deletion_remeasurement"]
    assert hermeting[0]["status"] == "clean"


def _project_delete_met_stappen(
    monkeypatch: pytest.MonkeyPatch, release_fout: Exception | None = None
) -> tuple[DeleteProjectManager, MagicMock]:
    """Een delete_project-ronde met een voortgangsmanager; de deployments zijn afgevangen."""
    deployment = _make_deployment()
    pm = _make_project_manager_mock()
    pm.get_contents = AsyncMock(return_value=_make_project_data(deployments=[deployment]))
    pm.get_deployments = AsyncMock(return_value=[deployment])
    pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.resource_exists = AsyncMock(return_value=False)
    pm._kubectl_connector.namespace_absent = AsyncMock(return_value=True)
    pm._kubectl_connector.wait_for_resource_absence = AsyncMock(return_value=True)

    progress = MagicMock()
    progress.add_task = MagicMock(side_effect=lambda name, subject=None: f"stap-{name}")
    pm.get_progress_manager = MagicMock(return_value=progress)

    manager = DeleteProjectManager(pm)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_project = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    monkeypatch.setattr(
        manager, "delete_deployment", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )
    for naam in (
        "_cleanup_project_infrastructure",
        "_cleanup_project_keycloak_realm",
        "_delete_project_level_folder",
        "_delete_project_argocd_folder",
        "_delete_project_namespaces",
    ):
        monkeypatch.setattr(manager, naam, AsyncMock(return_value=True))
    monkeypatch.setattr(
        manager,
        "_release_project_applications",
        AsyncMock(side_effect=release_fout) if release_fout else AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        manager, "_delete_project_file", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )
    return manager, progress


async def test_een_project_verwijderen_meldt_de_stappen_van_de_projectlaag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De deployments zelf zijn hier afgevangen; het gaat om de stappen eromheen."""
    manager, progress = _project_delete_met_stappen(monkeypatch)

    resultaat = await manager.delete_project("test-project")

    assert resultaat["success"] is True, resultaat["errors"]
    assert _stappen_van(progress) == [
        ("Projectinfrastructuur opruimen", None),
        ("Keycloak-realm verwijderen", None),
        ("ArgoCD-resources van het project verwijderen", None),
        ("Namespaces verwijderen", None),
        ("Controleren dat alles verwijderd is", None),
        ("Projectbestand verwijderen", None),
        ("Subdomeinen opruimen", None),
    ]
    assert progress.complete_task.call_count == 7


async def test_een_fout_bij_het_vrijgeven_van_de_applications_meldt_de_stap_mislukt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_project vangt de fout af en meldt de rit mislukt; de stap mag niet open of
    groen blijven hangen."""
    manager, progress = _project_delete_met_stappen(monkeypatch, release_fout=RuntimeError("argo git is stuk"))

    resultaat = await manager.delete_project("test-project")

    assert resultaat["success"] is False
    assert any("argo git is stuk" in fout for fout in resultaat["errors"]), resultaat["errors"]
    progress.fail_task.assert_called_once_with("stap-ArgoCD-resources van het project verwijderen", "argo git is stuk")
    # Alleen de stappen vóór de mislukte stap kleurden groen.
    assert progress.complete_task.call_count == 2
