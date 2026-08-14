"""
Tests for the kubectl connector.
"""

import base64
import json
import os
import tempfile
from pathlib import Path
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


class TestPatchSecretData:
    """Patching a secret must reach kubectl through a file, never through argv.

    This connector already keeps secret values out of the log; the argument list of
    the kubectl process is the other place they would be readable. The patch is a
    merge patch on purpose: the secrets in a project namespace are ArgoCD's, and an
    apply would rewrite the last-applied-configuration and drop its tracking labels.
    """

    async def test_values_go_in_through_a_file_and_not_through_argv(self, connector) -> None:
        captured: dict[str, str] = {}

        async def _run(args: list[str], **kwargs: object) -> tuple[str, str, int]:
            patch_path = args[args.index("--patch-file") + 1]
            captured["args"] = " ".join(args)
            captured["patch"] = Path(patch_path).read_text()
            return ("secret/main-database patched", "", 0)

        with patch.object(connector, "_run_kubectl_command", new=AsyncMock(side_effect=_run)):
            await connector.patch_secret_data("main-database", "rig-demo", {"DATABASE_PASSWORD": "Sup3rSecret"})

        assert "Sup3rSecret" not in captured["args"]
        assert "--type merge" in captured["args"]
        assert "-p" not in captured["args"].split()

        payload = json.loads(captured["patch"])
        assert payload == {"data": {"DATABASE_PASSWORD": base64.b64encode(b"Sup3rSecret").decode()}}

    async def test_patch_file_is_removed_even_when_kubectl_fails(self, connector) -> None:
        seen: list[str] = []

        async def _run(args: list[str], **kwargs: object) -> tuple[str, str, int]:
            seen.append(args[args.index("--patch-file") + 1])
            return ("", 'Error from server (NotFound): secrets "main-database" not found', 1)

        with (
            patch.object(connector, "_run_kubectl_command", new=AsyncMock(side_effect=_run)),
            pytest.raises(KubectlExecutionError, match="NotFound"),
        ):
            await connector.patch_secret_data("main-database", "rig-demo", {"DATABASE_PASSWORD": "x"})

        assert not Path(seen[0]).exists()


if __name__ == "__main__":
    pytest.main([__file__])
