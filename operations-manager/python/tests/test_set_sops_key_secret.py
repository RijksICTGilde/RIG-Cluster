"""Tests for scripts/sops_key_secret.py, the one irreversible step of the cutover.

The failure this file exists to prevent: ``sops-age-key`` is not one secret in one namespace.
Measured on the sandbox cluster, 12 namespaces hold a secret by that name -- two with the
platform key and ten with a per-project key that OPI wrote there
(``store_project_sops_key_in_namespace``). A tool that replaces every ``sops-age-key`` would
overwrite ten projects' own keys with the platform key and make their secrets unreadable.

So the selection is by KEY, not by name, and that is what is measured here. The rest of the
checks cover the guards that make the step safe to rerun and hard to run by accident.
"""

from __future__ import annotations

import base64
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opi.utils.sops import generate_sops_key_pair

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sops_key_secret as tool  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")


def _secret(namespace: str, private_key: str) -> dict:
    """A secret as kubectl returns it: the whole key file, base64 encoded, under ``key``."""
    content = f"# created: today\n# public key: whatever\n{private_key}\n"
    return {
        "metadata": {"name": "sops-age-key", "namespace": namespace},
        "data": {"key": base64.b64encode(content.encode()).decode()},
    }


def test_the_public_key_is_read_back_out_of_the_secret() -> None:
    private, public = generate_sops_key_pair()
    assert tool.public_key_from_secret_data(_secret("rig-system", private)["data"]["key"]) == public


def test_an_unreadable_secret_value_yields_none() -> None:
    assert tool.public_key_from_secret_data(base64.b64encode(b"no key in here").decode()) is None
    assert tool.public_key_from_secret_data("not-base64-at-all!!") is None


def test_only_the_namespaces_on_the_old_platform_key_are_selected(capsys: pytest.CaptureFixture) -> None:
    """The measurement that matters: a project's own key is left alone.

    Without the key comparison this selection would return all four namespaces, and the two
    project keys would be overwritten with the platform key.
    """
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    project_one_private, project_one_public = generate_sops_key_pair()
    project_two_private, _project_two_public = generate_sops_key_pair()
    payload = {
        "kind": "SecretList",
        "items": [
            _secret("rig-prd-operations", old_private),
            _secret("rig-prd-ron", old_private),
            _secret("rig-prd-cot-zaq", project_one_private),
            _secret("rig-prd-amtbz", project_two_private),
        ],
    }

    holders = tool.holders_from_secret_list(payload)
    targets = tool.report_holders(holders, old_public, new_public)

    assert [holder.namespace for holder in targets] == ["rig-prd-operations", "rig-prd-ron"]
    printed = capsys.readouterr().out
    assert "2 carry the OLD platform key and WILL be replaced" in printed
    assert "2 carry a DIFFERENT key and are left alone" in printed
    assert project_one_public in printed
    del new_private


def test_a_namespace_already_on_the_new_key_is_not_selected(capsys: pytest.CaptureFixture) -> None:
    """Makes the step safe to rerun: a second run finds nothing to do."""
    old_private, old_public = generate_sops_key_pair()
    new_private, new_public = generate_sops_key_pair()
    payload = {"kind": "SecretList", "items": [_secret("rig-prd-operations", new_private)]}

    targets = tool.report_holders(tool.holders_from_secret_list(payload), old_public, new_public)

    assert targets == []
    assert "1 already carry the new platform key" in capsys.readouterr().out
    del old_private


def test_a_single_secret_response_is_handled_as_well() -> None:
    """``kubectl get`` returns a bare Secret rather than a list when it is asked for one."""
    private, public = generate_sops_key_pair()
    payload = {"kind": "Secret", **_secret("rig-system", private)}
    holders = tool.holders_from_secret_list(payload)
    assert [(holder.namespace, holder.public_key) for holder in holders] == [("rig-system", public)]


@pytest.mark.asyncio
async def test_the_secret_keeps_the_whole_key_file_as_its_value(tmp_path: Path) -> None:
    """sops-plugin.sh greps ``^AGE-SECRET-KEY-`` out of the value, so the file shape must survive."""
    private, _public = generate_sops_key_pair()
    key_file = tmp_path / "key.txt"
    key_file.write_text(f"# created: today\n# public key: x\n{private}\n")
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=("rendered: yaml", "", 0))

    with patch.object(tool, "create_kubectl_connector", return_value=connector):
        await tool.write_secret("rig-prd-operations", key_file)

    create_args = connector.run_command.await_args_list[0].args[0]
    assert f"--from-file=key={key_file}" in create_args
    assert "--dry-run=client" in create_args
    apply_args = connector.run_command.await_args_list[1].args[0]
    assert apply_args == ["apply", "-n", "rig-prd-operations", "-f", "-"]
    assert connector.run_command.await_args_list[1].kwargs["stdin_input"] == "rendered: yaml"


@pytest.mark.asyncio
async def test_a_namespace_without_the_deployment_is_not_restarted() -> None:
    """The RON namespace holds the key but runs no operations-manager; that is not an error."""
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=("", "NotFound", 1))

    with patch.object(tool, "create_kubectl_connector", return_value=connector):
        assert await tool.restart_operations_manager("rig-prd-ron") is False

    assert connector.run_command.await_count == 1


@pytest.mark.asyncio
async def test_the_restart_waits_for_the_rollout_to_be_ready() -> None:
    """A restart that is not waited on lets the smoke test run against the old pod."""
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=("ok", "", 0))

    with patch.object(tool, "create_kubectl_connector", return_value=connector):
        assert await tool.restart_operations_manager("rig-prd-operations") is True

    called = [call.args[0] for call in connector.run_command.await_args_list]
    assert called[1][:2] == ["rollout", "restart"]
    assert called[2][:2] == ["rollout", "status"]
    assert "--timeout=180s" in called[2]


@pytest.mark.asyncio
async def test_the_listing_uses_a_field_selector() -> None:
    """kubectl refuses "get secret <name> --all-namespaces"; measured, it is an error."""
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=('{"kind": "SecretList", "items": []}', "", 0))

    with patch.object(tool, "create_kubectl_connector", return_value=connector):
        assert await tool.find_holders() == []

    args = connector.run_command.await_args_list[0].args[0]
    assert "--all-namespaces" in args
    assert "--field-selector=metadata.name=sops-age-key" in args
    assert "sops-age-key" not in args[:3]


@pytest.mark.asyncio
async def test_a_wrong_cluster_name_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The confirmation is the only guard on an irreversible step, so it has to bite."""
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=("", "", 0))

    with (
        patch.object(tool, "current_cluster", AsyncMock(return_value="odcn-production")),
        patch.object(
            tool,
            "find_holders",
            AsyncMock(return_value=[tool.SecretHolder("rig-prd-operations", old_public)]),
        ),
        patch.object(tool, "create_kubectl_connector", return_value=connector),
    ):
        code = await tool.main(
            [
                "--old-key",
                str(tmp_path / "old_key.txt"),
                "--new-key",
                str(tmp_path / "key.txt"),
                "--confirm-cluster",
                "kind-rig-sandbox",
            ]
        )

    assert code == 1
    assert "cluster name does not match" in capsys.readouterr().err
    connector.run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    old_private, old_public = generate_sops_key_pair()
    new_private, _new_public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=("", "", 0))

    with (
        patch.object(tool, "current_cluster", AsyncMock(return_value="odcn-production")),
        patch.object(
            tool,
            "find_holders",
            AsyncMock(return_value=[tool.SecretHolder("rig-prd-operations", old_public)]),
        ),
        patch.object(tool, "create_kubectl_connector", return_value=connector),
    ):
        code = await tool.main(
            [
                "--dry-run",
                "--old-key",
                str(tmp_path / "old_key.txt"),
                "--new-key",
                str(tmp_path / "key.txt"),
                "--confirm-cluster",
                "odcn-production",
            ]
        )

    assert code == 0
    assert "Dry run: nothing was changed." in capsys.readouterr().out
    connector.run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_same_key_twice_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    private, _public = generate_sops_key_pair()
    (tmp_path / "old_key.txt").write_text(f"{private}\n")
    (tmp_path / "key.txt").write_text(f"{private}\n")

    code = await tool.main(
        [
            "--old-key",
            str(tmp_path / "old_key.txt"),
            "--new-key",
            str(tmp_path / "key.txt"),
            "--confirm-cluster",
            "whatever",
        ]
    )

    assert code == 2
    assert "the same key" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_missing_key_file_names_the_path(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = await tool.main(
        [
            "--old-key",
            str(tmp_path / "gone.txt"),
            "--new-key",
            str(tmp_path / "gone.txt"),
            "--confirm-cluster",
            "whatever",
        ]
    )
    assert code == 2
    assert str(tmp_path / "gone.txt") in capsys.readouterr().err
