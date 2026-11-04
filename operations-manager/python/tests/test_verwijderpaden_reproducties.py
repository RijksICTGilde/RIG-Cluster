"""Reproducties van de vijf verwijderfouten van oktober 2026 (RC-379).

Vier van de vijf zijn sinds RC-385 groen en staan niet meer op ``xfail``. Geval 3 blijft
``xfail(strict=True)``: dat is invariant I5, het rapport van sectie 6.4 van het onderzoek,
en dat hoort bij een volgende kaart. De volledige kaart van de verwijderpaden, met het
bewijs per geval, staat in ``features/futures/verwijderpaden-in-kaart.md``.

De vijf gevallen; vier gemeten op de sandbox, geval 4 op productie:

1. ``isoa8-v4i`` - het verwijderen van de laatste deployment haalt het AppProject-bestand
   weg en laat de projectapplicatie ``{project}-project`` staan, die ernaar wijst.
2. ``rig-rl437-56m`` - de namespace bleef en het projectbestand ging: ``force`` loopt door
   naar stap 5 ook als de namespace niet verwijderd is.
3. ``swfa1-maw`` - een half aangemaakt project laat een projectmap achter die de
   orphan-sweep ``expected`` noemt, omdat het project nog bestaat.
4. ``nd-j7s`` (productie) - de veegactie van het forceren inventariseert elk namespaced
   type, dus in een Capsule-tenant is ze altijd onvolledig en meldt de taak "not fully
   deleted" terwijl de verwijdering afkwam.
5. ``rl263-ofb`` en twee andere - de grendel "namespace still used by other deployments"
   leest het projectbestand, dus een zusterdeployment die in DEZELFDE ronde wordt
   verwijderd houdt de namespace tegen. Drie van de vijf projecten hielden hun namespace.
"""

from __future__ import annotations

import copy
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.utils.naming import (
    generate_argocd_appproject_prefix,
    generate_argocd_project_application_name,
    generate_gitops_argocd_application_path,
    get_output_filename_from_template,
)
from tests.test_zombie_deployment import _delete, _delete_manager

if TYPE_CHECKING:
    from pathlib import Path

    from opi.manager.delete_project_manager import DeleteProjectManager

# ---------------------------------------------------------------------------
# Geval 1: een AppProject verdwijnt terwijl de projectapplicatie ernaar wijst
# ---------------------------------------------------------------------------


def _schrijf_projectmap(werkmap: Path, project: str, namespace: str, deployment: str) -> Path:
    """De argo-projectmap zoals het aanmaken hem achterlaat, met beide applicaties erin."""
    project_dir = werkmap / "local" / project
    project_dir.mkdir(parents=True)

    (werkmap / generate_gitops_argocd_application_path("local", project, deployment)).write_text("kind: Application\n")

    appproject_name = generate_argocd_appproject_prefix(project, namespace)
    (project_dir / get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)).write_text(
        yaml.safe_dump({"kind": "AppProject", "metadata": {"name": appproject_name}})
    )

    project_app_name = generate_argocd_project_application_name(project)
    (project_dir / get_output_filename_from_template("argocd-application.yaml.jinja", project_app_name)).write_text(
        yaml.safe_dump(
            {
                "kind": "Application",
                "metadata": {"name": project_app_name},
                "spec": {"project": appproject_name},
            }
        )
    )
    return project_dir


def _appprojects_en_verwijzingen(project_dir: Path) -> tuple[set[str], dict[str, str]]:
    """Welke AppProjects deze map definieert, en welke Application naar welke verwijst."""
    gedefinieerd: set[str] = set()
    verwijzingen: dict[str, str] = {}
    for pad in sorted(project_dir.glob("*.yaml")):
        document = yaml.safe_load(pad.read_text())
        if not isinstance(document, dict):
            continue
        naam = (document.get("metadata") or {}).get("name", "")
        if document.get("kind") == "AppProject":
            gedefinieerd.add(naam)
        elif document.get("kind") == "Application":
            verwijzingen[naam] = (document.get("spec") or {}).get("project", "")
    return gedefinieerd, verwijzingen


async def test_de_projectapplicatie_houdt_het_appproject_in_de_map(tmp_path: Path) -> None:
    manager: DeleteProjectManager
    manager, gitops, manifests = _delete_manager()

    werkmap = tmp_path / "argo-applications"
    project_dir = _schrijf_projectmap(werkmap, "test-project", "test-project", "pr-42")
    gitops.get_working_dir = AsyncMock(return_value=str(werkmap))
    manifests.get_working_dir = AsyncMock(return_value=str(tmp_path / "deploy-repo"))

    await _delete(manager, exists=os.path.exists)

    gedefinieerd, verwijzingen = _appprojects_en_verwijzingen(project_dir)
    hangend = {naam: project for naam, project in verwijzingen.items() if project not in gedefinieerd}
    assert not hangend, (
        f"deze Application(s) wijzen naar een AppProject dat deze map niet meer definieert: {hangend}; "
        f"de map definieert {gedefinieerd or 'geen enkel AppProject'}"
    )


# ---------------------------------------------------------------------------
# Geval 3: een half aangemaakte projectmap heet 'expected'
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "swfa1-maw: het aanmaken schrijft de projectmap in twee commits, en de tweede (de "
        "Application van de deployment) bleef uit. De orphan-sweep kijkt alleen of er nog een "
        "project met die naam BESTAAT, en dat is zo, dus de half gevulde map heet 'expected' en "
        "niets meldt dat de deployment van het projectbestand geen Application heeft"
    ),
)
async def test_de_sweep_meldt_een_deployment_zonder_application(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.jobs import service_orphan_sweep

    project_yaml = {
        "name": "swfa1-maw",
        "deployments": [{"name": "productie", "cluster": "sandboxed-local", "namespace": "swfa1-maw"}],
    }
    # Alleen de projectapplicatie; de Application van 'productie' is er nooit gekomen.
    gitops = AsyncMock()
    gitops.list_tree = AsyncMock(return_value=["sandboxed-local/swfa1-maw/swfa1-maw-project-argocd-application.yaml"])
    monkeypatch.setattr(service_orphan_sweep, "get_git_repo", MagicMock(return_value=gitops), raising=True)
    monkeypatch.setattr(service_orphan_sweep, "credentials_from_repo_config", MagicMock(), raising=True)
    # Alleen de GitOps-sectie wordt hier gemeten; de andere inventarissen leveren niets,
    # zodat hun connectorfouten het rapport niet vervuilen.
    leeg = AsyncMock()
    leeg.list_databases = AsyncMock(return_value=[])
    leeg.list_realms = AsyncMock(return_value=[])
    leeg.list_buckets = AsyncMock(return_value=[])
    monkeypatch.setattr(service_orphan_sweep, "create_postgres_connector", MagicMock(return_value=leeg))
    monkeypatch.setattr(service_orphan_sweep, "create_minio_connector", MagicMock(return_value=leeg))
    monkeypatch.setattr(service_orphan_sweep, "create_keycloak_connector", AsyncMock(return_value=leeg))
    marks = MagicMock()
    marks.return_value.get_all_marks = AsyncMock(return_value=[])
    monkeypatch.setattr(service_orphan_sweep, "MarkedForDeletionService", marks, raising=True)

    rapport = await service_orphan_sweep.sweep([project_yaml], "sandboxed-local")

    mappen = {item["path"]: item for item in rapport["gitops_folders"]}
    gemeld = mappen["sandboxed-local/swfa1-maw"]["classification"]
    assert gemeld != "expected", (
        "de map mist de Application van deployment 'productie' en wordt toch als in orde gemeld "
        f"(classification={gemeld!r})"
    )


# ---------------------------------------------------------------------------
# Geval 2, tweede helft: het projectbestand gaat weg terwijl de namespace blijft
# ---------------------------------------------------------------------------


async def test_een_achtergebleven_namespace_houdt_het_projectbestand(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.manager.delete_project_manager import DeleteProjectManager
    from tests.test_delete_project_manager import _make_deployment, _make_project_data, _make_project_manager_mock

    deployment = _make_deployment()
    pm = _make_project_manager_mock()
    pm.get_contents = AsyncMock(return_value=_make_project_data(deployments=[deployment]))
    pm.get_deployments = AsyncMock(return_value=[deployment])

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))

    manager = DeleteProjectManager(pm)
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)

    subdomain = MagicMock()
    subdomain.return_value.delete_by_project = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    # Precies de uitkomst die rl437-56m gaf: de namespace is niet verwijderd omdat een
    # (nog in het projectbestand staande) andere deployment hem volgens de toets gebruikt.
    achtergebleven = {
        "success": False,
        "operations": [
            {
                "type": "namespace_deletion",
                "target": "rig-test-project",
                "status": "skipped",
                "reason": "Namespace still used by other deployments",
            }
        ],
        "errors": ["Deployment 'pr-42' was kept in the project file: the ArgoCD application was not confirmed deleted"],
    }

    async def _mislukte_deployment_delete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return achtergebleven

    monkeypatch.setattr(manager, "delete_deployment", _mislukte_deployment_delete)
    for naam in ["_cleanup_project_infrastructure", "_cleanup_project_keycloak_realm", "_delete_project_argocd_folder"]:
        monkeypatch.setattr(manager, naam, AsyncMock())
    projectbestand_weg = AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    monkeypatch.setattr(manager, "_delete_project_file", projectbestand_weg)

    await manager.delete_project("test-project", force=True)

    projectbestand_weg.assert_not_awaited()


# ---------------------------------------------------------------------------
# Geval 4: de veegactie vraagt elk namespaced type, dus in een tenant nooit compleet
# ---------------------------------------------------------------------------

#: Zoals op productie: 322 namespaced typen, waarvan de serviceaccount er 92 niet mag
#: lijsten in een Capsule-tenant. Hier drie van elk, met dezelfde verhouding in gedrag.
_TENANT_FORBIDDEN_TYPES = ["podtemplates", "elasticsearches.elasticsearch.k8s.io", "kibanas.kibana.k8s.io"]

#: De kinds die laag 3 rendert en die de Application dus bezit; alle drie wel te lijsten.
_EIGEN_TYPES = ["deployments.apps", "services", "secrets"]


async def test_de_veegactie_vraagt_alleen_de_typen_van_de_application() -> None:
    from opi.manager.delete_project_manager import DeleteProjectManager

    gevraagd: list[list[str] | None] = []

    async def _list_tracked_resources(
        namespace: str, resource_types: list[str] | None = None, own_types: list[str] | None = None
    ) -> tuple[list[Any], bool]:
        gevraagd.append(resource_types)
        gevraagde = resource_types if resource_types is not None else _EIGEN_TYPES + _TENANT_FORBIDDEN_TYPES
        # Zoals kubectl: exitcode 1 als EEN van de typen weigert, de rest wordt wel gedrukt.
        return [], not any(naam in _TENANT_FORBIDDEN_TYPES for naam in gevraagde)

    kubectl = AsyncMock()
    kubectl.get_argocd_application_destination_namespace = AsyncMock(return_value="rig-prd-nd-j7s")
    kubectl.argocd_application_resource_types = AsyncMock(return_value=_EIGEN_TYPES)
    kubectl.list_namespaced_resource_types = AsyncMock(return_value=_EIGEN_TYPES + _TENANT_FORBIDDEN_TYPES)
    kubectl.list_tracked_resources = AsyncMock(side_effect=_list_tracked_resources)
    kubectl.delete_tracked_resources = AsyncMock(return_value=[])
    kubectl.list_argocd_applications = AsyncMock(return_value=[{"metadata": {"name": "nd-j7s-pr-61"}}])
    kubectl.remove_argocd_application_finalizers = AsyncMock(return_value=True)

    pm = AsyncMock()
    pm._kubectl_connector = kubectl
    manager = DeleteProjectManager(pm)

    deletion_results: dict[str, Any] = {"operations": [], "errors": []}
    uitkomst = await manager._force_delete_stuck_application("nd-j7s-pr-61", deletion_results)

    assert not uitkomst.swept_partially, (
        "de veegactie noemt zichzelf onvolledig terwijl alleen typen weigerden die ZAD niet "
        f"aanmaakt; gevraagde typen: {gevraagd}, fouten: {deletion_results['errors']}"
    )


# ---------------------------------------------------------------------------
# Geval 5: de grendel op de namespace leest het projectbestand, niet de ronde
# ---------------------------------------------------------------------------


def _twee_deployments_in_een_namespace() -> list[dict[str, Any]]:
    """Zoals de reallife-suite een project achterlaat: de wizard-deployment en een
    tweede die later is toegevoegd, beide in de projectnamespace."""
    return [
        {
            "name": naam,
            "cluster": "local",
            "namespace": "test-project",
            "repository": "main-repo",
            "components": [{"reference": "web"}],
        }
        for naam in ("productie", "acceptatie")
    ]


def _project_manager_met_levend_projectbestand(bestand: dict[str, Any]) -> AsyncMock:
    """Een ProjectManager waarvan het projectbestand echt meebeweegt met stap 8.

    De grendel leest ``get_contents()``, dus een mock die altijd hetzelfde teruggeeft
    zou de meting zelf maken. Hier beslist het bestand, net als in productie.
    """
    pm = AsyncMock()
    pm.get_name = AsyncMock(return_value=bestand["name"])
    # Geen voortgangsmanager: de delete-stappen melden dan niets.
    pm.get_progress_manager = MagicMock(return_value=None)
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
    pm.get_argo_workspace = AsyncMock(return_value=gitops)

    manifests = AsyncMock()
    manifests.get_working_dir = AsyncMock(return_value="/tmp/deploy-repo")
    pm.get_deployment_workspace = AsyncMock(return_value=manifests)

    async def _get_contents(**kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(bestand)

    async def _get_deployments(cluster_filter: bool = True) -> list[dict[str, Any]]:
        return copy.deepcopy(bestand["deployments"])

    async def _get_deployment_by_name(naam: str) -> dict[str, Any] | None:
        for deployment in bestand["deployments"]:
            if deployment.get("name") == naam:
                return copy.deepcopy(deployment)
        return None

    async def _mutate_and_commit(mutator: Any, commit_message: str, **kwargs: Any) -> bool:
        gemuteerd = mutator(copy.deepcopy(bestand))
        if gemuteerd is None:
            return False
        bestand.clear()
        bestand.update(gemuteerd)
        return True

    pm.get_contents = AsyncMock(side_effect=_get_contents)
    pm.get_deployments = AsyncMock(side_effect=_get_deployments)
    pm.get_deployment_by_name = AsyncMock(side_effect=_get_deployment_by_name)
    pm.mutate_and_commit_project = AsyncMock(side_effect=_mutate_and_commit)
    return pm


async def test_een_projectbrede_verwijdering_ruimt_de_namespace_op(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.manager.delete_project_manager import DeleteProjectManager, StuckApplicationOutcome

    bestand: dict[str, Any] = {
        "name": "test-project",
        "services": [],
        "deployments": _twee_deployments_in_een_namespace(),
        "repositories": [{"name": "main-repo", "path": ""}],
    }
    pm = _project_manager_met_levend_projectbestand(bestand)
    manager = DeleteProjectManager(pm)

    store = MagicMock()
    store.get = MagicMock(return_value=MagicMock(filename="test-project.yaml"))
    monkeypatch.setattr("opi.manager.delete_project_manager.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.delete_project_manager.settings.CLUSTER_MANAGER", "local", raising=False)
    subdomain = MagicMock()
    subdomain.return_value.delete_by_deployment = AsyncMock(return_value=0)
    subdomain.return_value.delete_by_project = AsyncMock(return_value=0)
    monkeypatch.setattr("opi.manager.delete_project_manager.SubdomainConnector", subdomain)

    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=True)
    # De Application gaat niet weg en de veegactie krijgt de finalizer er niet af: precies
    # 'kept its finalizer after a partial sweep' uit de meting van 23-10.
    argo.wait_for_application_deletion = AsyncMock(return_value=False)
    monkeypatch.setattr("opi.manager.delete_project_manager.create_argo_connector", lambda: argo)
    monkeypatch.setattr(
        manager,
        "_force_delete_or_await_cascade",
        AsyncMock(return_value=StuckApplicationOutcome(application_gone=False, finalizer_removed=False)),
    )
    for naam in ["_cleanup_project_infrastructure", "_cleanup_project_keycloak_realm", "_delete_project_argocd_folder"]:
        monkeypatch.setattr(manager, naam, AsyncMock())
    monkeypatch.setattr(
        manager, "_delete_project_file", AsyncMock(return_value={"success": True, "operations": [], "errors": []})
    )

    with patch("os.path.exists", return_value=False), patch("shutil.rmtree", MagicMock()):
        resultaat = await manager.delete_project("test-project", force=True)

    overgeslagen = [
        op for op in resultaat["operations"] if op["type"] == "namespace_deletion" and op["status"] == "skipped"
    ]
    assert pm._kubectl_connector.delete_namespace.await_args_list, (
        "geen enkele beurt heeft de namespace verwijderd terwijl het hele project weggaat; "
        f"overgeslagen: {[op['reason'] for op in overgeslagen]}, "
        f"entries die in het projectbestand bleven: {[d['name'] for d in bestand['deployments']]}"
    )
