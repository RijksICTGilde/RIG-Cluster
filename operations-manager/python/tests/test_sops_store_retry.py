"""The SOPS-key store retries the transient Capsule-RBAC race on a freshly created
namespace, and only logs an ERROR once it has genuinely given up.

A newly created tenant namespace's Capsule admin RoleBinding takes ~1s to
propagate; the first apply then fails with a transient Forbidden. That must not
raise an alert if a retry succeeds.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.kubectl import KubectlExecutionError
from opi.handlers.sops import SopsHandler


def _handler_with(apply_side_effect):
    kubectl = MagicMock()
    kubectl.apply_manifest = AsyncMock(side_effect=apply_side_effect)
    return SopsHandler(kubectl_connector=kubectl), kubectl


@pytest.mark.asyncio
async def test_transient_forbidden_then_success_does_not_error():
    handler, kubectl = _handler_with(
        [KubectlExecutionError("... secrets is forbidden: cannot get resource ..."), None]
    )
    with (
        patch("opi.handlers.sops.asyncio.sleep", new=AsyncMock()) as sleep,
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "AGE-SECRET-KEY-1x", "age1x")

    assert ok is True
    assert kubectl.apply_manifest.await_count == 2  # retried once
    sleep.assert_awaited()  # backed off between attempts
    log.error.assert_not_called()  # the transient race is not an error


@pytest.mark.asyncio
async def test_persistent_forbidden_errors_after_exhausting_retries():
    handler, kubectl = _handler_with(KubectlExecutionError("... is forbidden ..."))
    with (
        patch("opi.handlers.sops.asyncio.sleep", new=AsyncMock()),
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "k", "p")

    assert ok is False
    assert kubectl.apply_manifest.await_count == 4  # _SOPS_STORE_MAX_ATTEMPTS
    log.error.assert_called_once()


@pytest.mark.asyncio
async def test_non_transient_error_fails_fast_without_retry():
    handler, kubectl = _handler_with(KubectlExecutionError("Error: unable to recognize InvalidKind"))
    with (
        patch("opi.handlers.sops.asyncio.sleep", new=AsyncMock()),
        patch.object(handler, "logger") as log,
    ):
        ok = await handler.store_project_sops_key_in_namespace("rig-prd-x", "k", "p")

    assert ok is False
    assert kubectl.apply_manifest.await_count == 1  # no retry on a non-transient error
    log.error.assert_called_once()
