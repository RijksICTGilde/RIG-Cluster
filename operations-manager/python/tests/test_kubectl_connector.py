"""
Tests for the kubectl connector.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector, create_kubectl_connector


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset KubectlConnector singleton between tests."""
    KubectlConnector._instance = None
    KubectlConnector._initialized = False
    yield
    KubectlConnector._instance = None
    KubectlConnector._initialized = False


@pytest.fixture
def connector():
    """Create a KubectlConnector with asyncio.create_task patched out."""
    with patch("opi.connectors.kubectl.asyncio.create_task", new=MagicMock()):
        return KubectlConnector()


@pytest.fixture
def manifest_file():
    """Create a temporary manifest file for testing."""
    temp_dir = tempfile.TemporaryDirectory()
    manifest_path = os.path.join(temp_dir.name, "test_manifest.yaml")

    with open(manifest_path, "w") as f:
        f.write("""apiVersion: v1
kind: Namespace
metadata:
  name: {{ namespace }}
  labels:
    argocd.argoproj.io/managed-by: {{ manager }}
    created-by: operations-manager
""")

    yield manifest_path
    temp_dir.cleanup()


@pytest.fixture
def variables():
    return {"namespace": "test-project", "manager": "rig-system"}


def test_create_kubectl_connector():
    """Test creating a kubectl connector."""
    with patch("opi.connectors.kubectl.asyncio.create_task", new=MagicMock()):
        connector = create_kubectl_connector()
        assert isinstance(connector, KubectlConnector)


def test_template_manifest(connector, manifest_file, variables):
    """Test templating a manifest with variables."""
    with open(manifest_file) as f:
        manifest_content = f.read()

    result = connector.template_manifest(manifest_content, variables)

    assert "name: test-project" in result
    assert "argocd.argoproj.io/managed-by: rig-system" in result


async def test_apply_manifest(connector, manifest_file, variables):
    """Test applying a manifest."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("namespace/test-project created", "", 0)

        result = await connector.apply_manifest(manifest_file, variables)

        assert result is True
        mock_run_cmd.assert_called_once()
        args = mock_run_cmd.call_args[0][0]
        assert args[0] == "apply"
        assert args[1] == "-f"


async def test_apply_manifest_failure(connector, manifest_file, variables):
    """Test applying a manifest with a failure."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "Error: unable to recognize", 1)

        result = await connector.apply_manifest(manifest_file, variables)

        assert result is False


if __name__ == "__main__":
    pytest.main([__file__])
