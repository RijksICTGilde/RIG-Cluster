"""
Integration test fixtures.

This module provides fixtures specific to integration tests and real service
connections. The Kubernetes cluster the kubernetes_live and argocd_live groups measure on
comes from ``task test-cluster-up``, not from a fixture: fase 2 made it one cluster that
several groups share, and a fixture that creates and deletes it per session cannot do that.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from opi.bootstrap.keycloak_setup import KeycloakSetup
from opi.connectors.keycloak import create_keycloak_connector
from opi.core.config import settings
from opi.manager.argo_manager import ArgoManager
from opi.manager.project_manager import ProjectManager
from tests.integration import argocd_live, forgejo_live, keycloak_image, kubernetes_live

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from opi.connectors.keycloak import KeycloakConnector


def is_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False


# Register custom markers
def pytest_configure(config: Any) -> None:
    """Register custom pytest markers for integration tests."""
    config.addinivalue_line("markers", "integration: marks tests as integration tests")


@pytest.fixture
def docker_compose_file() -> str:
    """Return the path to the docker-compose.dev.yaml file."""
    return str(Path(__file__).parent.parent.parent.parent.parent / "docker-compose.dev.yaml")


def _wait_for_postgres(compose_file: Path) -> None:
    """Wait for postgres to be ready."""
    max_retries = 30
    for _i in range(max_retries):
        health_result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "-U",
                "opi",
            ],
            capture_output=True,
            text=True,
        )
        if health_result.returncode == 0:
            print("Postgres is ready")
            return
        time.sleep(1)

    pytest.fail("Postgres did not become ready in time")


@pytest.fixture(scope="session")
def docker_services(docker_compose_file: str) -> None:
    """
    Start Docker services defined in docker-compose.dev.yaml.

    This fixture starts postgres and any other required services
    for integration tests that need a real database.

    Note: Service cleanup is intentionally omitted to allow reuse
    during development. Use 'task dev-down' to clean up.
    """
    if not is_docker_available():
        pytest.skip("Docker not available")

    compose_file = Path(__file__).parent.parent.parent.parent.parent / "docker-compose.dev.yaml"

    if not compose_file.exists():
        pytest.skip("docker-compose.dev.yaml not found")

    print("\nStarting Docker services...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "postgres"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        pytest.skip(f"Failed to start Docker services: {result.stderr}")

    _wait_for_postgres(compose_file)


# ==================== De groep keycloak_live ====================
#
# Alle modules met de marker ``keycloak_live`` meten tegen dezelfde echte Keycloak, die
# `task test-keycloak` opstart. Deze fixtures staan daarom hier en niet in een van die
# modules: ze zijn modulebreed, en een tweede exemplaar per module zou een tweede
# admin-token en een tweede bootstrap betekenen. Ze materialiseren alleen voor een toets
# die erom vraagt, dus de rest van deze integratietoetsen merkt er niets van.


@pytest.fixture(scope="module")
def keycloak_url() -> str:
    url = keycloak_image.basis_url()
    keycloak_image.assert_local(url)
    return url


@pytest.fixture(scope="module")
def api(keycloak_url: str) -> keycloak_image.AdminApi:
    return keycloak_image.AdminApi(keycloak_url)


@pytest.fixture(scope="module")
def patched_settings(keycloak_url: str) -> Generator[None]:
    """Point OPI's settings at the throwaway Keycloak for the whole module."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "KEYCLOAK_URL", keycloak_url)
        patch.setattr(settings, "KEYCLOAK_ADMIN_USERNAME", keycloak_image.omgeving("KEYCLOAK_IMAGE_TEST_ADMIN"))
        patch.setattr(
            settings, "KEYCLOAK_ADMIN_PASSWORD", keycloak_image.omgeving("KEYCLOAK_IMAGE_TEST_ADMIN_PASSWORD")
        )
        patch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_ID", "opi-admin-service")
        patch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", keycloak_image.SERVICE_ACCOUNT_SECRET)
        yield


@pytest.fixture(scope="module")
async def connector(patched_settings: None, api: keycloak_image.AdminApi) -> KeycloakConnector:
    """A connector on client credentials, after OPI's own bootstrap created the account.

    The bootstrap step is the production path: it uses the admin password once to create the
    master service-account client, and everything afterwards runs on that account.
    """
    await KeycloakSetup().ensure_master_admin_service_account()
    client = api.client("master", "opi-admin-service")
    assert client["serviceAccountsEnabled"] is True
    return await create_keycloak_connector()


# ==================== De groep argocd_live ====================
#
# Alle modules met de marker ``argocd_live`` meten tegen dezelfde ArgoCD op het
# wegwerpcluster, die `task test-cluster-up` neerzet. De opstelling staat daarom hier en
# niet in een van die modules: hij kost een echte sync, en een tweede exemplaar per module
# zou die twee keer doen. Ze materialiseren alleen voor een toets die erom vraagt.


@pytest.fixture(scope="module")
def argo_kubectl() -> kubernetes_live.Kubectl:
    """De Kubernetes-API: de waarheid over wat ArgoCD werkelijk neerzette."""
    kubernetes_live.assert_test_cluster()
    return kubernetes_live.Kubectl()


@pytest.fixture(scope="module")
def argo_settings() -> Generator[None]:
    """De instellingen van OPI op de ArgoCD van het wegwerpcluster, voor de hele module.

    Ook ``CLUSTER_MANAGER``, want de managers leiden de ArgoCD-namespace daaruit af, en op
    dit cluster staat ArgoCD waar hij op de sandbox staat.
    """
    argocd_live.assert_local()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")
        patch.setattr(settings, "ARGOCD_HOST", argocd_live.host())
        patch.setattr(settings, "ARGOCD_PORT", argocd_live.port())
        patch.setattr(settings, "ARGOCD_TOKEN", argocd_live.token())
        patch.setattr(settings, "ARGOCD_USERNAME", "admin")
        patch.setattr(settings, "ARGOCD_PASSWORD", argocd_live.admin_password())
        patch.setattr(settings, "ARGOCD_USE_TLS", argocd_live.use_tls())
        patch.setattr(settings, "ARGOCD_VERIFY_SSL", False)
        yield


@pytest.fixture(scope="module")
def argo_forgejo() -> Generator[forgejo_live.ForgejoApi]:
    """De Forgejo van het wegwerpcluster, waar de manifesten in staan."""
    api = forgejo_live.ForgejoApi()
    yield api
    api.drop_token()


@pytest.fixture(scope="module")
async def deployed_app(
    argo_settings: None,
    argo_kubectl: kubernetes_live.Kubectl,
    argo_forgejo: forgejo_live.ForgejoApi,
) -> AsyncGenerator[argocd_live.DeployedApp]:
    """Een Application die ArgoCD echt heeft uitgerold, langs de weg van productie.

    De manifesten komen uit ``ArgoManager``: de AppProject en de Application zijn die van
    productie, met de CMP-plugin en het syncPolicy-blok eromheen, want juist daar zitten de
    valkuilen die de scenario's meten (PruneLast, selfHeal, prune).

    De umbrella-Application die ze in productie toepast, bestaat op dit cluster niet; de
    opstelling doet die ene stap met kubectl.
    """
    opstelling = argocd_live.DeployedApp(
        app=f"{argocd_live.PREFIX}-app",
        app_project=f"{argocd_live.PREFIX}-project",
        namespace=f"{argocd_live.PREFIX}-argo",
        repo=f"{argocd_live.PREFIX}-manifests",
        repo_url=argocd_live.repo_url_in_cluster(forgejo_live.admin_user(), f"{argocd_live.PREFIX}-manifests"),
        workload=f"{argocd_live.PREFIX}-web",
    )
    argocd_live.drop_everything(opstelling, argo_kubectl, argo_forgejo)

    argo_forgejo.fresh_repo(
        opstelling.repo,
        files={
            "kustomization.yaml": "resources:\n  - deployment.yaml\n",
            "deployment.yaml": argocd_live.workload_manifest(
                opstelling.workload, opstelling.namespace, kubernetes_live.WORKLOAD_IMAGE
            ),
        },
    )
    await argocd_live.prepare_destination_namespace(opstelling.namespace, argo_kubectl)
    argo_kubectl.apply(
        argocd_live.repository_secret_manifest(
            f"{argocd_live.PREFIX}-repo", opstelling.repo_url, forgejo_live.admin_user(), argo_forgejo.token
        )
    )

    manager = ArgoManager(ProjectManager())
    argo_kubectl.apply(
        manager.generate_appproject_manifest(
            name=opstelling.app_project,
            namespace=argocd_live.ARGO_NAMESPACE,
            destination_namespace=opstelling.namespace,
            project_label=argocd_live.PREFIX,
            source_repos=[opstelling.repo_url],
        )
    )
    argo_kubectl.apply(
        manager.generate_application_manifest(
            name=opstelling.app,
            namespace=argocd_live.ARGO_NAMESPACE,
            argo_project=opstelling.app_project,
            repo_url=opstelling.repo_url,
            target_revision="main",
            repo_path=".",
            destination_namespace=opstelling.namespace,
            project_label=argocd_live.PREFIX,
        )
    )

    await manager.wait_for_application_created(opstelling.app)
    await manager.wait_for_application_synced(opstelling.app)

    yield opstelling

    argocd_live.drop_everything(opstelling, argo_kubectl, argo_forgejo)
