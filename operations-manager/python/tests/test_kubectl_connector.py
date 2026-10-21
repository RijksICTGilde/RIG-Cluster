"""
Tests for the kubectl connector.
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError, create_kubectl_connector
from ruamel.yaml import YAML


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


async def test_encrypt_file_with_sops_gebruikt_de_gefilterde_omgeving(connector, tmp_path, monkeypatch):
    """RC-279: deze aanroep had geen env, en erfde dus de volle omgeving van OPI zelf."""
    monkeypatch.setenv("DATABASE_ADMIN_PASSWORD", "geheim")
    connector.env = {"PATH": "/usr/bin", "HOME": "/home/opi"}

    source = tmp_path / "plain.yaml"
    source.write_text("key: value\n")
    output = tmp_path / "encrypted.yaml"

    mock_process = MagicMock()
    mock_process.communicate = AsyncMock(return_value=(b"versleuteld", b""))
    mock_process.returncode = 0

    with patch(
        "opi.connectors.kubectl.asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_process)
    ) as mock_exec:
        result = await connector.encrypt_file_with_sops(str(source), "age1publiek", str(output))

    assert result is True
    assert output.read_text() == "versleuteld"
    assert mock_exec.call_args.kwargs["env"] == connector.env
    assert "DATABASE_ADMIN_PASSWORD" not in mock_exec.call_args.kwargs["env"]


async def test_apply_labels_to_resource_sets_all_labels_in_one_call(connector):
    """RC-316: namespace creation sets managed-by and Pod Security labels together."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "", 0)

        result = await connector.apply_labels_to_resource(
            resource_type="namespace",
            resource_name="rig-myproject",
            labels={"argocd.argoproj.io/managed-by": "rig-system", "pod-security.kubernetes.io/enforce": "baseline"},
        )

        assert result is True
        mock_run_cmd.assert_called_once()
        args = mock_run_cmd.call_args[0][0]
        assert args[:3] == ["label", "namespace", "rig-myproject"]
        assert "argocd.argoproj.io/managed-by=rig-system" in args
        assert "pod-security.kubernetes.io/enforce=baseline" in args
        assert "--overwrite" in args
        assert "-n" not in args, "namespace is cluster-scoped, no -n flag"


async def test_apply_labels_to_resource_with_no_labels_skips_kubectl(connector):
    """An empty dict (e.g. a cluster without pod_security configured) is a no-op, not a call."""
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        assert await connector.apply_labels_to_resource(resource_type="namespace", resource_name="x", labels={})

        mock_run_cmd.assert_not_called()


async def test_apply_labels_to_resource_retries_then_succeeds(connector):
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.side_effect = [("", "conflict", 1), ("", "", 0)]

        result = await connector.apply_labels_to_resource(
            resource_type="namespace", resource_name="x", labels={"a": "b"}, retry_delay=0
        )

        assert result is True
        assert mock_run_cmd.call_count == 2


async def test_apply_labels_to_resource_fails_after_exhausting_retries(connector):
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "Forbidden", 1)

        result = await connector.apply_labels_to_resource(
            resource_type="namespace", resource_name="x", labels={"a": "b"}, max_retries=2, retry_delay=0
        )

        assert result is False
        assert mock_run_cmd.call_count == 2


async def test_apply_labels_to_resource_passes_the_namespace_for_a_namespaced_resource(connector):
    with patch.object(connector, "_run_kubectl_command", new_callable=AsyncMock) as mock_run_cmd:
        mock_run_cmd.return_value = ("", "", 0)

        assert await connector.apply_labels_to_resource(
            resource_type="pod", resource_name="x", labels={"a": "b"}, namespace="rig-myproject"
        )

        args = mock_run_cmd.call_args[0][0]
        assert "a=b" in args
        assert args[-2:] == ["-n", "rig-myproject"]


if __name__ == "__main__":
    pytest.main([__file__])


def test_template_manifest_renders_generic_secret(connector):
    """The generic secret template uses ``| yaml_scalar``; a bare Template has no such filter."""
    with open("manifests/generic-secret.yaml.to-sops.jinja") as f:
        manifest_content = f.read()

    result = connector.template_manifest(
        manifest_content,
        {
            "name": "mail-platform",
            "namespace": "rig-system",
            "secret_type": "mail",
            "secret_k8s_type": "Opaque",
            "secret_pairs": {"username": "zad", "password": 'p\\a"s'},
        },
    )

    # Op de geparste waarden: het filter bepaalt de quoting, dus de tekstvorm is geen contract.
    stringdata = YAML(typ="safe", pure=True).load(result)["stringData"]
    assert stringdata == {"username": "zad", "password": 'p\\a"s'}


FAKE_KUBECTL = """#!/usr/bin/env python3
import os
import pathlib
import sys

# Own command line plus that of every ancestor: an intermediate /bin/sh is what carries the
# input in its argv, so dumping only this process would miss exactly the leak under test.
cmdlines = []
pid = os.getpid()
while pid > 1:
    cmdlines.append(pathlib.Path(f"/proc/{pid}/cmdline").read_bytes())
    pid = int(pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
pathlib.Path(os.environ["KUBECTL_ARGV_DUMP"]).write_bytes(b"\\n".join(cmdlines))
pathlib.Path(os.environ["KUBECTL_STDIN_DUMP"]).write_bytes(sys.stdin.buffer.read())
"""


@pytest.fixture
def fake_kubectl(connector, tmp_path):
    """A real ``kubectl`` on PATH that dumps its argv and its stdin, so a test can assert
    what the process table showed and what arrived through the pipe."""
    script = tmp_path / "kubectl"
    script.write_text(FAKE_KUBECTL)
    script.chmod(0o755)
    connector.env = connector.env | {"PATH": f"{tmp_path}:{os.environ['PATH']}"}
    KubectlConnector.isConnected = True

    argv_dump = tmp_path / "argv"
    stdin_dump = tmp_path / "stdin"
    env = {"KUBECTL_ARGV_DUMP": str(argv_dump), "KUBECTL_STDIN_DUMP": str(stdin_dump)}
    return env, argv_dump, stdin_dump


async def test_stdin_input_travels_through_a_pipe_and_never_through_the_arguments(connector):
    """The old path wrapped the input in a heredoc for ``create_subprocess_shell``, which put
    the whole manifest (project keys, client secrets) in the argv of ``/bin/sh -c``."""
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(b"applied", b""))
    process.returncode = 0
    KubectlConnector.isConnected = True

    with (
        patch(
            "opi.connectors.kubectl.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as exec_mock,
        patch("opi.connectors.kubectl.asyncio.create_subprocess_shell") as shell_mock,
    ):
        await connector._run_kubectl_command(["apply", "-f", "-"], stdin_input="kind: Secret\n")

    shell_mock.assert_not_called()
    assert exec_mock.call_args.args == ("kubectl", "apply", "-f", "-")
    assert exec_mock.call_args.kwargs["stdin"] is asyncio.subprocess.PIPE
    process.communicate.assert_awaited_once_with(input=b"kind: Secret\n")


async def test_a_command_without_input_opens_no_stdin_pipe(connector):
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(b"", b""))
    process.returncode = 0
    KubectlConnector.isConnected = True

    with patch(
        "opi.connectors.kubectl.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
    ) as exec_mock:
        await connector._run_kubectl_command(["get", "pods"])

    assert exec_mock.call_args.kwargs["stdin"] is None
    process.communicate.assert_awaited_once_with(input=None)


async def test_a_manifest_containing_a_line_eof_is_data_and_not_a_command(connector, fake_kubectl, tmp_path):
    """Under the heredoc a line reading exactly ``EOF`` ended the input and the shell ran the
    rest. The templates quote their values today, but the connector may not depend on that."""
    env, _argv_dump, stdin_dump = fake_kubectl
    sentinel = tmp_path / "pwned"
    manifest = f"kind: ConfigMap\nEOF\ntouch {sentinel}\n"

    stdout, stderr, code = await connector.run_command(["apply", "-f", "-"], env=env, stdin_input=manifest)

    assert (stdout, stderr, code) == ("", "", 0)
    assert stdin_dump.read_text() == manifest
    assert not sentinel.exists(), "the input after the EOF line was executed by a shell"


async def test_no_secret_from_the_input_appears_in_the_argv_of_the_process(connector, fake_kubectl):
    """Measured on the old path: the marker was visible in ``/proc/<pid>/cmdline`` of
    ``/bin/sh`` for as long as the apply ran."""
    env, argv_dump, _stdin_dump = fake_kubectl
    marker = "AGE-SECRET-KEY-1KANARIE"

    await connector.run_command(["apply", "-f", "-"], env=env, stdin_input=f"kind: Secret\ndata:\n  key: {marker}\n")

    assert marker not in argv_dump.read_text()


async def test_an_input_of_several_megabytes_is_no_longer_bounded_by_the_argument_length(connector, fake_kubectl):
    """As a shell argument this hit the kernel limit (``E2BIG``); through a pipe it does not."""
    env, _argv_dump, stdin_dump = fake_kubectl
    manifest = "kind: ConfigMap\ndata:\n  blob: " + "x" * (4 * 1024 * 1024) + "\n"

    _stdout, _stderr, code = await connector.run_command(["apply", "-f", "-"], env=env, stdin_input=manifest)

    assert code == 0
    assert stdin_dump.read_text() == manifest
