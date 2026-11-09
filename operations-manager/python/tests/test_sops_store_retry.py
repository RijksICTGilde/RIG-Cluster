"""The SOPS-key store retries the transient Capsule-RBAC race on a freshly created
namespace, and leaves the ERROR to its caller.

A newly created tenant namespace's Capsule admin RoleBinding takes ~1s to propagate; the
first apply then fails with a transient Forbidden. That must not raise an alert if a retry
succeeds. The whole retry lives here, and giving up stays a WARNING: the caller in
project_manager turns a False into its own ERROR and raises.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlExecutionError
from opi.handlers.sops import SopsHandler
from opi.manager.project_manager import ProjectManager


def _handler_with(apply_side_effect):
    kubectl = MagicMock()
    kubectl.apply_manifest = AsyncMock(side_effect=apply_side_effect)
    return SopsHandler(kubectl_connector=kubectl), kubectl


@pytest.mark.asyncio
async def test_transient_forbidden_then_success_does_not_error():
    handler, kubectl = _handler_with(
        [KubectlExecutionError("... secrets is forbidden: cannot get resource ..."), None, None]
    )
    with (
        patch("asyncio.sleep", new=AsyncMock()) as sleep,
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "AGE-SECRET-KEY-1x", "age1x")

    assert ok is True
    sjablonen = [call.args[0] for call in kubectl.apply_manifest.await_args_list]
    assert sjablonen.count("manifests/generic-secret.yaml.to-sops.jinja") == 2  # retried once
    assert sjablonen.count("manifests/cmp-sops-key-reader.yaml.jinja") == 1  # de Role gaat met het secret mee
    sleep.assert_awaited()  # backed off between attempts
    log.error.assert_not_called()  # the transient race is not an error


@pytest.mark.asyncio
async def test_persistent_forbidden_warns_after_exhausting_retries():
    handler, kubectl = _handler_with(KubectlExecutionError("... is forbidden ..."))
    with (
        patch("asyncio.sleep", new=AsyncMock()),
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "k", "p")

    assert ok is False
    assert kubectl.apply_manifest.await_count == 7  # one series over _SOPS_STORE_BUDGET_SECONDS
    log.error.assert_not_called()  # the caller raises and logs the ERROR
    log.warning.assert_called()


@pytest.mark.asyncio
async def test_non_transient_error_fails_fast_without_retry():
    handler, kubectl = _handler_with(KubectlExecutionError("Error: unable to recognize InvalidKind"))
    with (
        patch("asyncio.sleep", new=AsyncMock()),
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "k", "p")

    assert ok is False
    assert kubectl.apply_manifest.await_count == 1  # no retry on a non-transient error
    log.error.assert_not_called()  # the caller raises and logs the ERROR for both cases
    log.warning.assert_called()


@pytest.mark.asyncio
async def test_a_failing_role_apply_does_not_retry_the_secret():
    handler, kubectl = _handler_with(
        [None, KubectlExecutionError("... roles.rbac.authorization.k8s.io is forbidden ...")]
    )
    with (
        patch("asyncio.sleep", new=AsyncMock()),
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "k", "p")

    assert ok is False  # the Role failed, even though the Secret itself was stored
    assert kubectl.apply_manifest.await_count == 2  # secret once, role once, no retry of either
    log.error.assert_called_once()
    melding = log.error.call_args.args[0]
    assert "CMP key reader role" in melding  # de melding gaat over de Role, niet over het secret
    assert "SOPS key stored" in melding


@pytest.mark.asyncio
async def test_one_loop_covers_the_race_and_the_caller_does_not_retry_again():
    """The handler owns the whole budget; the caller turns its False into one failure."""
    kubectl = MagicMock(
        apply_manifest=AsyncMock(side_effect=KubectlExecutionError("... is forbidden ...")),
        get_sops_secret_from_namespace=AsyncMock(return_value=None),
        delete_resource=AsyncMock(),
    )
    manager = ProjectManager.__new__(ProjectManager)
    manager._kubectl_connector = kubectl
    manager._sops_handler = SopsHandler(kubectl_connector=kubectl)

    with (
        patch("opi.manager.project_manager.get_project_public_key", return_value="age1x"),
        patch("opi.manager.project_manager.get_decoded_project_private_key", new=AsyncMock(return_value="k")),
        patch("asyncio.sleep", new=AsyncMock()),
        pytest.raises(RuntimeError),
    ):
        await manager._ensure_sops_secret_in_namespace("rig-prd-demo", {"name": "demo"})

    # 1, 2, 4, 8, 16, 29 seconds of waiting: one series, not ten times a short one.
    assert kubectl.apply_manifest.await_count == 7
