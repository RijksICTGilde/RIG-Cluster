"""OPI's ArgoCD-connector tegen een echte ArgoCD in de gepinde versie.

Elke methode van ``ArgoConnector`` draait hier ten minste een keer, over dezelfde REST-API
en met dezelfde authenticatie als OPI: het API-token van het account ``opi``, en de
inlogroute met gebruiker en wachtwoord. Voor deze connector bestond geen enkele echte
toets; ``test_argocd_stuck_delete.py`` alleen al heeft 220 mocks.

Het terugleeskanaal is de Kubernetes-API (``kubernetes_live.Kubectl``): wat ArgoCD meldt is
een mening, wat het cluster heeft staan is de waarheid. De scenario's over meer dan een stap
staan in ``test_argocd_scenarios.py``.

Draai hem met ``task test-cluster-up`` en dan ``task test-argocd``; zonder
``ARGOCD_LIVE_HOST`` slaat de groep zichzelf over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.connectors.argo import ArgoAuthConfigurationError, ArgoConnector, create_argo_connector
from tests.integration import argocd_live

if TYPE_CHECKING:
    from tests.integration.argocd_live import DeployedApp
    from tests.integration.kubernetes_live import Kubectl

pytestmark = [pytest.mark.requires_infra, pytest.mark.argocd_live]

ABSENT = f"{argocd_live.PREFIX}-bestaat-niet"


@pytest.fixture
def op_token(argo_settings: None) -> ArgoConnector:
    """De connector zoals OPI hem in productie bouwt: op het API-token van account opi."""
    return create_argo_connector()


@pytest.fixture
def op_login(argo_settings: None) -> ArgoConnector:
    """Dezelfde connector op de inlogroute, die het platform nog kent als terugval."""
    return create_argo_connector(token="")


# ==================== authenticatie ====================


async def test_an_api_token_is_the_authorization_and_needs_no_login(op_token: ArgoConnector) -> None:
    assert await op_token.login() is True
    assert op_token.auth_token == argocd_live.token()


async def test_login_with_username_and_password_gets_a_session_token(op_login: ArgoConnector) -> None:
    assert await op_login.login() is True
    assert op_login.auth_token
    assert op_login.auth_token != argocd_live.token()


async def test_login_answers_false_on_a_password_that_is_not_right(argo_settings: None) -> None:
    connector = create_argo_connector(token="", password="dit-is-niet-het-wachtwoord", connect_retry_seconds=0)
    assert await connector.login() is False


async def test_ensure_authenticated_gets_a_token_once(op_login: ArgoConnector) -> None:
    assert await op_login.ensure_authenticated() is True
    eerste = op_login.auth_token
    assert await op_login.ensure_authenticated() is True
    assert op_login.auth_token == eerste


def test_a_connector_without_any_credentials_refuses_to_be_built(argo_settings: None) -> None:
    """Vroeger stond hier admin/admin, dus een vergeten instelling probeerde stil het beheeraccount."""
    with pytest.raises(ArgoAuthConfigurationError):
        create_argo_connector(token="", username="", password="")


# ==================== lezen ====================


async def test_get_application_status_reports_what_the_cluster_has(
    op_token: ArgoConnector, argo_kubectl: Kubectl, deployed_app: DeployedApp
) -> None:
    status = await op_token.get_application_status(deployed_app.app)

    assert status is not None
    assert status["status"]["sync"]["status"] == "Synced"
    assert status["status"]["health"]["status"] == "Healthy"
    assert argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace) is not None


async def test_get_application_status_answers_none_for_an_application_that_is_not_there(
    op_token: ArgoConnector,
) -> None:
    """ArgoCD antwoordt 403 in plaats van 404 om niet te verklappen welke apps bestaan."""
    assert await op_token.get_application_status(ABSENT) is None


async def test_application_exists_follows_the_status(op_token: ArgoConnector, deployed_app: DeployedApp) -> None:
    assert await op_token.application_exists(deployed_app.app) is True
    assert await op_token.application_exists(ABSENT) is False


async def test_list_applications_includes_the_application_that_was_rolled_out(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    namen = {app["metadata"]["name"] for app in await op_token.list_applications()}

    assert deployed_app.app in namen


async def test_get_application_manifests_renders_what_the_repository_holds(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    """De API achter ``argocd app manifests``: een renderfout komt hier terug, niet pas later."""
    gelukt, body = await op_token.get_application_manifests(deployed_app.app)

    assert gelukt is True, body
    assert deployed_app.workload in body


async def test_get_application_manifests_answers_false_for_an_application_that_is_not_there(
    op_token: ArgoConnector,
) -> None:
    gelukt, _ = await op_token.get_application_manifests(ABSENT)
    assert gelukt is False


async def test_get_application_resource_tree_names_the_resources_that_really_stand(
    op_token: ArgoConnector, argo_kubectl: Kubectl, deployed_app: DeployedApp
) -> None:
    nodes = await op_token.get_application_resource_tree(deployed_app.app)

    soorten = {(node["kind"], node["name"]) for node in nodes}
    assert ("Deployment", deployed_app.workload) in soorten
    assert any(kind == "Pod" for kind, _ in soorten), "de boom hoort de pods eronder te bevatten"

    pods_volgens_k8s = argo_kubectl.names("pod", deployed_app.namespace)
    pods_volgens_argo = {naam for kind, naam in soorten if kind == "Pod"}
    assert pods_volgens_argo == pods_volgens_k8s


async def test_get_application_resource_tree_is_empty_for_an_application_that_is_not_there(
    op_token: ArgoConnector,
) -> None:
    assert await op_token.get_application_resource_tree(ABSENT) == []


# ==================== schrijven ====================


async def test_refresh_application_moves_the_reconciled_timestamp_forward(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    voor = (await op_token.get_application_status(deployed_app.app) or {})["status"]["reconciledAt"]

    na = await op_token.refresh_application(deployed_app.app)

    assert na is not None
    assert na >= voor


async def test_hard_refresh_application_also_answers_with_a_timestamp(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    """De dure variant: de manifestcache gaat leeg en de bron wordt opnieuw gerenderd."""
    assert await op_token.hard_refresh_application(deployed_app.app) is not None


async def test_refresh_application_answers_none_for_an_application_that_is_not_there(
    op_token: ArgoConnector,
) -> None:
    assert await op_token.refresh_application(ABSENT) is None


async def test_login_and_sync_is_a_refresh_of_the_named_application(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    assert await op_token.login_and_sync(deployed_app.app) is not None


async def test_sync_application_triggers_a_sync_that_ends(
    op_token: ArgoConnector, argo_kubectl: Kubectl, deployed_app: DeployedApp
) -> None:
    assert await op_token.sync_application(deployed_app.app) is True

    status = await op_token.get_application_status(deployed_app.app)
    assert status is not None
    assert status["status"]["sync"]["status"] == "Synced"
    assert argo_kubectl.get("deployment", deployed_app.workload, deployed_app.namespace) is not None


async def test_sync_application_answers_false_for_an_application_that_is_not_there(
    op_token: ArgoConnector,
) -> None:
    assert await op_token.sync_application(ABSENT) is False


async def test_wait_for_application_deletion_says_false_while_the_application_is_still_there(
    op_token: ArgoConnector, deployed_app: DeployedApp
) -> None:
    """De tegenhanger van de valkuil: 'weg' mag niet gemeld worden zolang hij er staat."""
    assert await op_token.wait_for_application_deletion(deployed_app.app, max_retries=2, retry_delay=1) is False
