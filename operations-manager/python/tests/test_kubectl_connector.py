"""
Tests for the kubectl connector.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError, create_kubectl_connector


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

        # Success returns None (no raise).
        assert await connector.apply_manifest(manifest_file, variables) is None
        mock_run_cmd.assert_called_once()
        args = mock_run_cmd.call_args[0][0]
        assert args[0] == "apply"
        assert args[1] == "-f"


async def test_apply_manifest_failure(connector, manifest_file, variables):
    """Test applying a manifest with a failure raises with the reason."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "Error: unable to recognize", 1)

        with pytest.raises(KubectlExecutionError, match="unable to recognize"):
            await connector.apply_manifest(manifest_file, variables)


async def test_a_failed_namespace_listing_raises_instead_of_answering_empty(connector):
    """argocd_orphan_sweep catches this exception to refuse (RC-226). Answering with an
    empty map instead would leave that except dead and the sweep would report SCHOON over a
    cluster it read nothing in, because without --namespace this map IS its work list."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "Error from server (Forbidden): namespaces is forbidden", 1)

        with pytest.raises(KubectlExecutionError, match="Forbidden"):
            await connector.get_namespace_label_map("created-by")


async def test_a_namespace_without_the_label_is_present_with_an_empty_value(connector):
    """``labels: null`` is how a namespace with no labels at all comes back, and
    argocd_orphan_sweep builds its allowlist from the value of every namespace it is given."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = (
            '{"items": [{"metadata": {"name": "rig-prd-mpfm-w3h", "labels": {"created-by": "operations-manager"}}},'
            ' {"metadata": {"name": "ingress-nginx", "labels": {"app": "x"}}},'
            ' {"metadata": {"name": "kube-node-lease", "labels": null}}]}',
            "",
            0,
        )

        assert await connector.get_namespace_label_map("created-by") == {
            "rig-prd-mpfm-w3h": "operations-manager",
            "ingress-nginx": "",
            "kube-node-lease": "",
        }


if __name__ == "__main__":
    pytest.main([__file__])
