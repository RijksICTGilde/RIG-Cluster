"""
Integration tests for kubectl log streaming with real Kind cluster.

These tests require:
- Kind CLI installed
- kubectl CLI installed
- Docker running

Run with: pytest tests/integration/test_kubectl_logs_real.py -v -m slow
"""

from typing import Any

import pytest

# Deze module praat met een echte Kubernetes-API via de Kind-cluster `rig-integration-test`
# (task test-kind-create). Gemarkeerd als requires_infra, want de standaard-addopts in
# pyproject.toml sluiten dat uit: zonder cluster gaven deze tests anders errors en ruim een
# minuut wachttijd in elke gewone run, terwijl niemand de opbrengst zag.
#
# De cluster ruimt zichzelf niet op (bewust, zie tests/integration/conftest.py) en kost twee
# Docker-containers van elk ~1GB. Draai `task test-kind-delete` als je klaar bent.
pytestmark = pytest.mark.requires_infra


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlLogsReal:
    """
    Integration tests using a real Kind cluster.

    These tests are marked as slow and require a Kind cluster to be
    available. They test the actual kubectl log streaming functionality.
    """

    async def test_get_deployment_logs(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test streaming logs from a real Kubernetes deployment."""
        connector = connected_kubectl_connector

        logs = await connector.get_deployment_logs(
            deployment_name="log-generator",
            namespace="test-project",
            lines=10,
        )

        # Verify we got some logs
        assert isinstance(logs, list)
        # Note: logs may be empty if deployment just started
        # The test passes if no exception is raised

    async def test_get_deployment_logs_nonexistent(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting logs from a nonexistent deployment."""
        connector = connected_kubectl_connector

        logs = await connector.get_deployment_logs(
            deployment_name="nonexistent-deployment",
            namespace="test-project",
            lines=10,
        )

        # Should return empty list for nonexistent deployment
        assert logs == []

    async def test_get_deployment_logs_nonexistent_namespace(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting logs from a nonexistent namespace."""
        connector = connected_kubectl_connector

        logs = await connector.get_deployment_logs(
            deployment_name="log-generator",
            namespace="nonexistent-namespace",
            lines=10,
        )

        # Should return empty list for nonexistent namespace
        assert logs == []

    async def test_namespace_exists(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test checking if a namespace exists."""
        connector = connected_kubectl_connector

        # test-project namespace should exist from test fixtures
        exists = await connector.namespace_exists("test-project")
        assert exists is True

    async def test_namespace_not_exists(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test checking if a nonexistent namespace exists."""
        connector = connected_kubectl_connector

        exists = await connector.namespace_exists("definitely-not-existing-namespace")
        assert exists is False

    async def test_get_deployment_status(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting deployment status from real cluster."""
        connector = connected_kubectl_connector

        status = await connector.get_deployment_status(
            namespace="test-project",
            deployment_name="log-generator",
        )

        # Should return list of deployment statuses
        assert isinstance(status, list)
        # Note: may be empty if deployment not yet created

    async def test_get_namespace_events(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting namespace events from real cluster."""
        connector = connected_kubectl_connector

        events = await connector.get_namespace_events(
            namespace="test-project",
            limit=10,
        )

        # Should return list of events
        assert isinstance(events, list)


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlCommandsReal:
    """Integration tests for kubectl commands with real Kind cluster."""

    async def test_run_command_get_namespaces(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running a kubectl get namespaces command."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(["get", "namespaces", "-o", "name"])

        assert code == 0
        assert "namespace/default" in stdout
        assert "namespace/kube-system" in stdout

    async def test_run_command_get_pods(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running a kubectl get pods command."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(["get", "pods", "-n", "test-project", "-o", "name"])

        # Command should succeed even if no pods exist yet
        assert code == 0

    async def test_run_command_cluster_info(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running kubectl cluster-info command."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(["cluster-info"])

        assert code == 0
        assert "Kubernetes" in stdout


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestConnectionRecovery:
    """Integration tests for kubectl connection recovery."""

    async def test_connection_is_established(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test that connection to Kind cluster is established."""
        connector = connected_kubectl_connector
        assert connector.isConnected is True

    async def test_multiple_commands_same_connection(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running multiple commands on the same connection."""
        connector = connected_kubectl_connector

        # Run multiple commands
        for _ in range(5):
            stdout, stderr, code = await connector.run_command(["get", "nodes", "-o", "name"])
            assert code == 0

        # Connection should still be active
        assert connector.isConnected is True
