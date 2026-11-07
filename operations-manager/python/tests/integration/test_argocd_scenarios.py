"""De ORDE van ArgoCD-stappen, met na elke stap de invariant tegen de Kubernetes-API.

Een losse aanroep kan kloppen terwijl de volgorde fout is: dat is waar de valkuilen zitten
die we op productie zijn tegengekomen. Drie daarvan hebben hier hun eigen scenario:

1. een delete die "geslaagd" meldt terwijl de resources er nog staan. De Kubernetes-API is
   de waarheid, niet de status van ArgoCD;
2. een delete die hangt op ``PruneLast``, de syncOption die het Application-sjabloon zet;
3. een refresh waarvan de status nog de oude stand toont, waardoor een ``Synced`` niets
   zegt over de commit die net gepusht is.

Gedraagt de gepinde versie zich goed, dan is de toets groen en bewaakt hij dat.

De manifesten komen uit ``ArgoManager`` en liggen in een repository op de Forgejo van het
testcluster, zoals productie dat uit zad-deployments doet. Draai hem met
``task test-cluster-up`` en dan ``task test-argocd``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from opi.connectors.argo import create_argo_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.manager.argo_manager import ArgoManager
from opi.manager.project_manager import ProjectManager
from tests.integration import argocd_live, kubernetes_live

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.integration.argocd_live import DeployedApp
    from tests.integration.forgejo_live import ForgejoApi
    from tests.integration.kubernetes_live import Kubectl

pytestmark = [pytest.mark.requires_infra, pytest.mark.argocd_live, pytest.mark.serial]


@pytest.fixture
def manager(argo_settings: None) -> ArgoManager:
    return ArgoManager(ProjectManager())


async def _wait_until(check: Callable[[], Awaitable[bool]], timeout: int = 180, interval: int = 2) -> bool:
    """Wacht op de toestand zelf; de teller is een vangnet, geen wachtmechanisme."""
    for _ in range(timeout // interval):
        if await check():
            return True
        await asyncio.sleep(interval)
    return False


async def test_what_argocd_calls_synced_and_healthy_really_stands_in_the_namespace(
    argo_kubectl: Kubectl, deployed_app: DeployedApp
) -> None:
    """De invariant onder alles hieronder: de twee bronnen zeggen hetzelfde.

    De opstelling heeft gewacht tot ArgoCD Synced en Healthy meldde. Dan hoort de werklast
    er echt te staan, met de replica's die het manifest in git noemt.
    """
    connector = create_argo_connector()
    status = await connector.get_application_status(deployed_app.app)

    assert status is not None
    assert status["status"]["sync"]["status"] == "Synced"
    assert status["status"]["health"]["status"] == "Healthy"

    deployment = argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace)
    assert deployment is not None
    assert deployment["status"]["readyReplicas"] == 1
    assert argo_kubectl.get("namespace", deployed_app.namespace) is not None


async def test_a_change_in_git_reaches_the_cluster_after_a_refresh(
    manager: ArgoManager, argo_kubectl: Kubectl, argo_forgejo: ForgejoApi, deployed_app: DeployedApp
) -> None:
    """Valkuil 3: een status die nog de oude stand toont.

    ``status_reflects_refresh`` vergelijkt ``reconciledAt`` met het tijdstip van onze eigen
    refresh; zonder dat bewijst een ``Synced`` niets over de commit die we net pushten.
    """
    connector = create_argo_connector()
    voor = (await connector.get_application_status(deployed_app.app) or {})["status"]["reconciledAt"]

    argo_forgejo.write_file(
        deployed_app.repo,
        "deployment.yaml",
        argocd_live.workload_manifest(
            deployed_app.workload, deployed_app.namespace, kubernetes_live.WORKLOAD_IMAGE, replicas=2
        ),
        message="twee replicas",
    )

    async def _reconciled_after_the_push() -> bool:
        status = await connector.get_application_status(deployed_app.app)
        return status is not None and manager.status_reflects_refresh(status, voor)

    gereconcilieerd = await connector.refresh_application(deployed_app.app)
    assert gereconcilieerd is not None
    assert await _reconciled_after_the_push(), "ArgoCD bleef de oude reconcile melden na onze refresh"

    await manager.wait_for_application_synced(deployed_app.app, refreshed_after=gereconcilieerd)

    deployment = argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace)
    assert deployment is not None
    assert deployment["spec"]["replicas"] == 2, "de wijziging uit git staat niet in het cluster"


async def test_a_hard_refresh_renders_the_source_again(
    argo_kubectl: Kubectl, argo_forgejo: ForgejoApi, deployed_app: DeployedApp
) -> None:
    """De dure variant, die de manifestcache leegt; na hem hoort de bron opnieuw gerenderd.

    Gemeten aan wat er uit komt en niet aan de aanroep: de werklast krijgt een label in git,
    en dat label hoort na de harde refresh in de gerenderde manifesten te staan.
    """
    connector = create_argo_connector()
    manifest = argocd_live.workload_manifest(
        deployed_app.workload, deployed_app.namespace, kubernetes_live.WORKLOAD_IMAGE
    ).replace(
        f"  name: {deployed_app.workload}\n",
        f"  name: {deployed_app.workload}\n  labels:\n    zadlive/ronde: twee\n",
        1,
    )
    argo_forgejo.write_file(deployed_app.repo, "deployment.yaml", manifest, message="label erbij")

    assert await connector.hard_refresh_application(deployed_app.app) is not None

    gelukt, body = await connector.get_application_manifests(deployed_app.app)
    assert gelukt is True, body
    assert "zadlive/ronde" in body

    assert await _wait_until(lambda: _has_label(argo_kubectl, deployed_app, "zadlive/ronde", "twee")), (
        "selfHeal heeft het gerenderde label niet uitgerold"
    )


async def _has_label(kubectl: Kubectl, app: DeployedApp, key: str, value: str) -> bool:
    deployment = kubectl.get("deployment", app.workload, app.namespace)
    return bool(deployment) and (deployment or {})["metadata"].get("labels", {}).get(key) == value


async def test_deleting_the_application_leaves_no_resource_in_the_namespace(
    argo_kubectl: Kubectl, deployed_app: DeployedApp
) -> None:
    """Valkuil 1 en 2 in een scenario: 'weg' moet weg zijn, en PruneLast mag niet hangen.

    Het Application-sjabloon zet ``PrunePropagationPolicy=foreground`` en ``PruneLast=true``,
    dus de cascade loopt via de finalizer van ArgoCD. Een delete die slaagt terwijl de
    resources er nog staan, is precies wat hier niet mag kunnen: de controle gaat daarom
    langs de Kubernetes-API en niet langs de status van ArgoCD.

    Deze toets ruimt de opstelling op; daarom staat hij als laatste, en draait deze module
    serieel.
    """
    connector = create_argo_connector()
    kubectl_connector = create_kubectl_connector()

    assert argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace) is not None

    argo_kubectl.ok("delete", "application", deployed_app.app, "-n", argocd_live.ARGO_NAMESPACE, "--wait=false")

    weg = await connector.wait_for_application_deletion(
        deployed_app.app, max_retries=60, retry_delay=3, kubectl_connector=kubectl_connector
    )
    assert weg is True, "de Application werd niet bevestigd verwijderd"

    assert await kubectl_connector.argocd_application_exists(deployed_app.app, argocd_live.ARGO_NAMESPACE) is False
    assert argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace) is None, (
        "ArgoCD meldde de Application weg terwijl de werklast er nog staat"
    )
    inventaris = await kubectl_connector.list_tracked_resources(deployed_app.namespace, ["deployments.apps", "pods"])
    assert inventaris is not None
    gevonden, volledig = inventaris
    assert volledig is True
    assert gevonden == [], f"ArgoCD liet resources staan: {gevonden}"


async def test_a_deletion_that_is_held_by_a_finalizer_is_not_reported_as_gone(
    manager: ArgoManager, argo_kubectl: Kubectl, argo_forgejo: ForgejoApi, deployed_app: DeployedApp
) -> None:
    """Valkuil 2 apart: zolang de finalizer de Application vasthoudt, is hij niet weg.

    Deze toets bouwt de opstelling zelf opnieuw op, want hij draait na de verwijdering
    hierboven. Dat maakt hem tegelijk de toets die bewijst dat aanmaken herhaalbaar is.
    """
    connector = create_argo_connector()
    kubectl_connector = create_kubectl_connector()

    argo_kubectl.apply(
        manager.generate_appproject_manifest(
            name=deployed_app.app_project,
            namespace=argocd_live.ARGO_NAMESPACE,
            destination_namespace=deployed_app.namespace,
            project_label=argocd_live.PREFIX,
            source_repos=[deployed_app.repo_url],
        )
    )
    argo_kubectl.apply(
        manager.generate_application_manifest(
            name=deployed_app.app,
            namespace=argocd_live.ARGO_NAMESPACE,
            argo_project=deployed_app.app_project,
            repo_url=deployed_app.repo_url,
            target_revision="main",
            repo_path=".",
            destination_namespace=deployed_app.namespace,
            project_label=argocd_live.PREFIX,
        )
    )
    await manager.wait_for_application_created(deployed_app.app)

    argo_kubectl.ok(
        "patch",
        "application",
        deployed_app.app,
        "-n",
        argocd_live.ARGO_NAMESPACE,
        "--type",
        "json",
        "-p",
        '[{"op":"add","path":"/metadata/finalizers/-","value":"zadlive.example/hold"}]',
    )
    argo_kubectl.ok("delete", "application", deployed_app.app, "-n", argocd_live.ARGO_NAMESPACE, "--wait=false")

    vastgehouden = await connector.wait_for_application_deletion(
        deployed_app.app, max_retries=3, retry_delay=2, kubectl_connector=kubectl_connector
    )
    assert vastgehouden is False, "een Application die een finalizer vasthoudt, is niet weg"
    assert argo_kubectl.get("application", deployed_app.app, argocd_live.ARGO_NAMESPACE) is not None

    assert (
        await kubectl_connector.remove_argocd_application_finalizers(deployed_app.app, argocd_live.ARGO_NAMESPACE)
        is True
    )
    assert (
        await connector.wait_for_application_deletion(
            deployed_app.app, max_retries=60, retry_delay=3, kubectl_connector=kubectl_connector
        )
        is True
    )
