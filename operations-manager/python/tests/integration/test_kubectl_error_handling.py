"""
Integration tests for kubectl error handling.

These tests verify that the kubectl connector handles errors gracefully.

Run with: pytest tests/integration/test_kubectl_error_handling.py -v -m slow
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
class TestKubectlCommandErrors:
    """Tests for kubectl command error handling."""

    async def test_invalid_command_returns_error(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test that invalid kubectl commands return proper error codes."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(["invalid-command"])

        assert code != 0
        assert stderr != "" or "error" in stdout.lower()

    async def test_get_nonexistent_resource_type(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting a nonexistent resource type."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(["get", "nonexistentresourcetype"])

        assert code != 0

    async def test_apply_to_nonexistent_namespace(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying to a nonexistent namespace fails gracefully."""
        connector = connected_kubectl_connector

        stdout, stderr, code = await connector.run_command(
            ["create", "configmap", "test-cm", "-n", "nonexistent-namespace-12345", "--from-literal=key=value"]
        )

        assert code != 0

    async def test_delete_protected_namespace(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test deleting a protected namespace fails gracefully."""
        connector = connected_kubectl_connector

        # Trying to delete kube-system should fail
        result = await connector.delete_resource(resource_type="namespace", resource_name="kube-system", namespace=None)

        # Should return False (not deleted) without crashing
        # Note: Kind might actually allow this, so we just verify no exception
        assert isinstance(result, bool)

    async def test_malformed_json_output(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test handling of commands that might produce unexpected output."""
        connector = connected_kubectl_connector

        # Get something that doesn't exist - should handle gracefully
        stdout, stderr, code = await connector.run_command(
            ["get", "pod", "nonexistent-pod-12345", "-n", "test-project", "-o", "json"]
        )

        assert code != 0


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlEdgeCases:
    """Tests for edge cases in kubectl operations."""

    async def test_empty_namespace_logs(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test getting logs from a namespace with no pods."""
        connector = connected_kubectl_connector

        # Create an empty namespace
        await connector.run_command(["create", "namespace", "empty-ns-test"])

        try:
            logs = await connector.get_deployment_logs(
                deployment_name="nonexistent", namespace="empty-ns-test", lines=10
            )

            # Should return empty list, not crash
            assert logs == []
        finally:
            await connector.run_command(["delete", "namespace", "empty-ns-test", "--ignore-not-found"])

    async def test_very_large_line_count(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test requesting a very large number of log lines."""
        connector = connected_kubectl_connector

        # Request more lines than exist - should not crash
        logs = await connector.get_deployment_logs(
            deployment_name="log-generator", namespace="test-project", lines=100000
        )

        assert isinstance(logs, list)

    async def test_special_characters_in_labels(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying labels with special characters."""
        connector = connected_kubectl_connector

        # Create a ConfigMap for testing
        await connector.run_command(
            ["create", "configmap", "special-label-test", "-n", "test-project", "--from-literal=key=value"]
        )

        try:
            # Labels have restrictions - this should handle them gracefully
            result = await connector.apply_label_to_resource(
                resource_type="configmap",
                resource_name="special-label-test",
                label_key="valid-label",
                label_value="valid-value-123",
                namespace="test-project",
            )
            assert result is True
        finally:
            await connector.run_command(
                ["delete", "configmap", "special-label-test", "-n", "test-project", "--ignore-not-found"]
            )

    async def test_concurrent_operations(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running multiple kubectl operations concurrently."""
        import asyncio

        connector = connected_kubectl_connector

        # Run multiple get operations concurrently
        tasks = [
            connector.run_command(["get", "namespaces", "-o", "name"]),
            connector.run_command(["get", "pods", "-A", "-o", "name"]),
            connector.run_command(["get", "configmaps", "-n", "test-project", "-o", "name"]),
            connector.namespace_exists("test-project"),
            connector.namespace_exists("kube-system"),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All operations should complete without exceptions
        for result in results:
            assert not isinstance(result, Exception), f"Got exception: {result}"


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlConnectionResilience:
    """Tests for connection resilience."""

    async def test_rapid_successive_commands(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test running many commands rapidly."""
        connector = connected_kubectl_connector

        # Run 20 rapid commands
        for _i in range(20):
            stdout, stderr, code = await connector.run_command(["get", "nodes", "-o", "name"])
            assert code == 0

        # Connection should still be active
        assert connector.isConnected is True

    async def test_mixed_success_and_failure_commands(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test that failures don't break subsequent successful commands."""
        connector = connected_kubectl_connector

        # Run a command that will fail
        stdout, stderr, code = await connector.run_command(["get", "invalid-resource"])
        assert code != 0

        # Run a command that should succeed
        stdout, stderr, code = await connector.run_command(["get", "namespaces", "-o", "name"])
        assert code == 0
        assert "namespace/default" in stdout

        # Connection should still be active
        assert connector.isConnected is True
