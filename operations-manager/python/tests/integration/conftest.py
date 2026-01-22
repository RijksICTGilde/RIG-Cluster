"""
Integration test fixtures.

This module provides fixtures specific to integration tests, including
Kind cluster management and real service connections.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


def is_kind_available() -> bool:
    """Check if Kind CLI is available."""
    try:
        result = subprocess.run(
            ["kind", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_kubectl_available() -> bool:
    """Check if kubectl CLI is available."""
    try:
        result = subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


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
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _create_kind_cluster(cluster_name: str, config_path: str) -> None:
    """Create a Kind cluster if it doesn't exist."""
    result = subprocess.run(
        ["kind", "get", "clusters"],
        capture_output=True,
        text=True,
    )

    if cluster_name in result.stdout.split():
        return  # Cluster already exists

    print(f"\nCreating Kind cluster: {cluster_name}")
    create_result = subprocess.run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            cluster_name,
            "--config",
            config_path,
            "--wait",
            "120s",
        ],
        capture_output=True,
        text=True,
    )

    if create_result.returncode != 0:
        pytest.fail(f"Failed to create Kind cluster: {create_result.stderr}")


def _deploy_test_workloads(workloads_path: str) -> None:
    """Deploy test workloads to the Kind cluster."""
    if not os.path.exists(workloads_path):
        return

    print(f"\nDeploying test workloads from: {workloads_path}")
    deploy_result = subprocess.run(
        ["kubectl", "apply", "-f", workloads_path],
        capture_output=True,
        text=True,
    )

    if deploy_result.returncode != 0:
        print(f"Warning: Failed to deploy test workloads: {deploy_result.stderr}")
        return

    print("\nWaiting for test deployment to be ready...")
    wait_result = subprocess.run(
        [
            "kubectl",
            "wait",
            "--for=condition=available",
            "deployment/log-generator",
            "-n",
            "test-project",
            "--timeout=120s",
        ],
        capture_output=True,
        text=True,
    )
    if wait_result.returncode != 0:
        print(f"Warning: Deployment may not be ready: {wait_result.stderr}")


# Register custom markers
def pytest_configure(config: Any) -> None:
    """Register custom pytest markers for integration tests."""
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "docker: marks tests that require Docker")
    config.addinivalue_line("markers", "kind: marks tests that require Kind cluster")


@pytest.fixture(scope="session")
def kind_cluster_name() -> str:
    """Return the name of the test Kind cluster."""
    return "rig-integration-test"


@pytest.fixture(scope="session")
def kind_config_path() -> str:
    """Return the path to the Kind cluster configuration."""
    return str(Path(__file__).parent.parent / "fixtures" / "kind-config.yaml")


@pytest.fixture(scope="session")
def test_workloads_path() -> str:
    """Return the path to the test workloads manifest."""
    return str(Path(__file__).parent.parent / "fixtures" / "test-workloads.yaml")


@pytest.fixture(scope="session")
def kind_cluster(
    kind_cluster_name: str,
    kind_config_path: str,
    test_workloads_path: str,
) -> str:
    """
    Create a Kind cluster for integration tests.

    This fixture is session-scoped and will:
    1. Create a Kind cluster with the specified configuration
    2. Deploy test workloads
    3. Wait for workloads to be ready

    Note: Cluster cleanup is intentionally omitted to allow reuse
    during development. Use 'task test-kind-delete' to clean up.

    Usage:
        @pytest.mark.slow
        @pytest.mark.kind
        def test_real_kubectl(kind_cluster):
            # kind_cluster is the cluster name
            ...

    Requires:
        - Kind CLI installed
        - kubectl CLI installed
        - Docker running
    """
    if not is_kind_available():
        pytest.skip("Kind CLI not available")

    if not is_kubectl_available():
        pytest.skip("kubectl CLI not available")

    if not is_docker_available():
        pytest.skip("Docker not available")

    _create_kind_cluster(kind_cluster_name, kind_config_path)

    # Set kubectl context
    subprocess.run(
        ["kubectl", "config", "use-context", f"kind-{kind_cluster_name}"],
        check=True,
    )

    _deploy_test_workloads(test_workloads_path)

    return kind_cluster_name


@pytest.fixture
def kubeconfig(kind_cluster: str) -> str:
    """
    Get kubeconfig content for the test cluster.

    Returns the kubeconfig as a string that can be written to a file
    or used with KUBECONFIG environment variable.
    """
    result = subprocess.run(
        ["kind", "get", "kubeconfig", "--name", kind_cluster],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def kubeconfig_file(kubeconfig: str, tmp_path: Path) -> str:
    """
    Create a temporary kubeconfig file for the test cluster.

    Returns the path to the kubeconfig file.
    """
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text(kubeconfig)
    return str(kubeconfig_path)


@pytest.fixture
def kubectl_env(kubeconfig_file: str) -> dict[str, str]:
    """
    Get environment variables for kubectl with the test cluster.

    Returns a dict that can be passed to subprocess.run(env=...).
    """
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig_file
    return env


@pytest.fixture
async def connected_kubectl_connector(kind_cluster: str) -> Any:
    """
    Get a KubectlConnector that is connected to the Kind test cluster.

    This fixture ensures the KubectlConnector singleton is properly
    initialized and connected to the test cluster.
    """
    from opi.connectors.kubectl import KubectlConnector

    # Reset singleton
    KubectlConnector._instance = None

    # Create new instance
    connector = KubectlConnector()

    # Wait for connection
    max_retries = 10
    for _i in range(max_retries):
        if connector.isConnected:
            break
        await asyncio.sleep(1)
    else:
        pytest.fail("Failed to connect to Kind cluster")

    return connector


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
