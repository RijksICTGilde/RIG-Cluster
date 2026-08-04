"""
Integration tests for kubectl write operations with real Kind cluster.

These tests verify that write operations (apply, delete, label) work correctly
against a real Kubernetes cluster.

Run with: pytest tests/integration/test_kubectl_write_ops.py -v -m slow
"""

import tempfile
from pathlib import Path
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
class TestKubectlApplyManifest:
    """Tests for applying Kubernetes manifests."""

    async def test_apply_simple_configmap(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying a simple ConfigMap manifest."""
        connector = connected_kubectl_connector

        manifest_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: test-configmap
  namespace: test-project
data:
  key1: value1
  key2: value2
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(manifest_content)
            manifest_path = f.name

        try:
            await connector.apply_manifest(manifest_path)  # success = no raise

            # Verify the ConfigMap was created
            stdout, stderr, code = await connector.run_command(
                ["get", "configmap", "test-configmap", "-n", "test-project", "-o", "name"]
            )
            assert code == 0
            assert "configmap/test-configmap" in stdout
        finally:
            # Cleanup
            Path(manifest_path).unlink(missing_ok=True)
            await connector.run_command(
                ["delete", "configmap", "test-configmap", "-n", "test-project", "--ignore-not-found"]
            )

    async def test_apply_manifest_with_variables(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying a manifest with template variables."""
        connector = connected_kubectl_connector

        manifest_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ name }}
  namespace: {{ namespace }}
data:
  environment: {{ environment }}
"""
        variables = {
            "name": "templated-configmap",
            "namespace": "test-project",
            "environment": "testing",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(manifest_content)
            manifest_path = f.name

        try:
            await connector.apply_manifest(manifest_path, variables)  # success = no raise

            # Verify the ConfigMap was created with correct values
            stdout, stderr, code = await connector.run_command(
                ["get", "configmap", "templated-configmap", "-n", "test-project", "-o", "jsonpath={.data.environment}"]
            )
            assert code == 0
            assert "testing" in stdout
        finally:
            Path(manifest_path).unlink(missing_ok=True)
            await connector.run_command(
                ["delete", "configmap", "templated-configmap", "-n", "test-project", "--ignore-not-found"]
            )

    async def test_apply_invalid_manifest(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying an invalid manifest raises."""
        from opi.connectors.kubectl import KubectlExecutionError

        connector = connected_kubectl_connector

        manifest_content = """
apiVersion: v1
kind: InvalidKind
metadata:
  name: will-fail
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(manifest_content)
            manifest_path = f.name

        try:
            with pytest.raises(KubectlExecutionError):
                await connector.apply_manifest(manifest_path)
        finally:
            Path(manifest_path).unlink(missing_ok=True)


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlDeleteOperations:
    """Tests for kubectl delete operations."""

    async def test_delete_resource(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test deleting a Kubernetes resource."""
        connector = connected_kubectl_connector

        # First create a resource
        stdout, stderr, code = await connector.run_command(
            ["create", "configmap", "delete-test-cm", "-n", "test-project", "--from-literal=key=value"]
        )
        assert code == 0

        # Delete it
        result = await connector.delete_resource(
            resource_type="configmap", resource_name="delete-test-cm", namespace="test-project"
        )
        assert result is True

        # Verify it's gone
        stdout, stderr, code = await connector.run_command(["get", "configmap", "delete-test-cm", "-n", "test-project"])
        assert code != 0  # Should fail because resource doesn't exist

    async def test_delete_nonexistent_resource(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test deleting a nonexistent resource."""
        connector = connected_kubectl_connector

        result = await connector.delete_resource(
            resource_type="configmap", resource_name="nonexistent-resource-12345", namespace="test-project"
        )
        # Should return False or True depending on implementation
        # The important thing is it doesn't raise an exception
        assert isinstance(result, bool)


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlLabelAnnotations:
    """Tests for applying labels and annotations."""

    async def test_apply_label_to_namespace(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying a label to a namespace."""
        connector = connected_kubectl_connector

        result = await connector.apply_label_to_resource(
            resource_type="namespace", resource_name="test-project", label_key="test-label", label_value="test-value"
        )
        assert result is True

        # Verify the label was applied
        stdout, stderr, code = await connector.run_command(
            ["get", "namespace", "test-project", "-o", "jsonpath={.metadata.labels.test-label}"]
        )
        assert code == 0
        assert "test-value" in stdout

    async def test_apply_annotation_to_configmap(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test applying an annotation to a ConfigMap."""
        connector = connected_kubectl_connector

        # Create a ConfigMap first
        await connector.run_command(
            [
                "create",
                "configmap",
                "annotation-test-cm",
                "-n",
                "test-project",
                "--from-literal=key=value",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        stdout, stderr, code = await connector.run_command(
            ["create", "configmap", "annotation-test-cm", "-n", "test-project", "--from-literal=key=value"]
        )

        try:
            result = await connector.apply_annotation_to_resource(
                resource_type="configmap",
                resource_name="annotation-test-cm",
                annotation_key="test-annotation",
                annotation_value="annotation-value",
                namespace="test-project",
            )
            assert result is True

            # Verify the annotation was applied
            stdout, stderr, code = await connector.run_command(
                [
                    "get",
                    "configmap",
                    "annotation-test-cm",
                    "-n",
                    "test-project",
                    "-o",
                    "jsonpath={.metadata.annotations.test-annotation}",
                ]
            )
            assert code == 0
            assert "annotation-value" in stdout
        finally:
            await connector.run_command(
                ["delete", "configmap", "annotation-test-cm", "-n", "test-project", "--ignore-not-found"]
            )


@pytest.mark.slow
@pytest.mark.kind
@pytest.mark.integration
@pytest.mark.asyncio
class TestKubectlSecrets:
    """Tests for secret operations."""

    async def test_get_secret(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test retrieving a secret."""
        connector = connected_kubectl_connector

        # Create a secret first
        stdout, stderr, code = await connector.run_command(
            [
                "create",
                "secret",
                "generic",
                "test-secret",
                "-n",
                "test-project",
                "--from-literal=username=admin",
                "--from-literal=password=secret123",
            ]
        )
        assert code == 0

        try:
            secret_data = await connector.get_secret("test-secret", "test-project")

            assert secret_data is not None
            assert "username" in secret_data
            assert "password" in secret_data
        finally:
            await connector.run_command(["delete", "secret", "test-secret", "-n", "test-project", "--ignore-not-found"])

    async def test_get_nonexistent_secret(
        self,
        connected_kubectl_connector: Any,
    ) -> None:
        """Test retrieving a nonexistent secret returns None."""
        connector = connected_kubectl_connector

        secret_data = await connector.get_secret("nonexistent-secret-12345", "test-project")
        assert secret_data is None
