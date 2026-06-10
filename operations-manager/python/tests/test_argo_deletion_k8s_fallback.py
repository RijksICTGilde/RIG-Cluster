"""Regression tests for ArgoConnector.wait_for_application_deletion.

The ArgoCD API returns an ambiguous 'permission denied' (PermissionError) to an
admin caller while it is merely stalled, for applications that still exist. The
old code treated that as "deleted", silently orphaning still-running deployments.

These tests pin the corrected behavior: ArgoCD is the fast path, but a
'permission denied' is never trusted as "deleted" - absence is confirmed against
the Kubernetes API (the honest source of truth) before reporting success.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.argo import ArgoConnector


def _kubectl_returning(value: bool | None) -> MagicMock:
    """A stand-in kubectl connector whose k8s existence check returns ``value``."""
    kubectl = MagicMock()
    kubectl.argocd_application_exists = AsyncMock(return_value=value)
    return kubectl


@pytest.mark.asyncio
async def test_permission_denied_confirmed_absent_via_k8s_is_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(False)  # k8s: NotFound -> truly gone
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is True
    kubectl.argocd_application_exists.assert_awaited()


@pytest.mark.asyncio
async def test_permission_denied_but_k8s_still_present_is_not_deleted() -> None:
    # The core regression: Argo says 'permission denied' but the CR still exists.
    # Old behavior returned True (false success); it must now return False.
    argo = ArgoConnector()
    kubectl = _kubectl_returning(True)  # k8s: still present
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is False


@pytest.mark.asyncio
async def test_permission_denied_k8s_unknown_is_not_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(None)  # k8s: could not determine -> must not claim deleted
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is False


@pytest.mark.asyncio
async def test_permission_denied_without_kubectl_cannot_confirm() -> None:
    # Without a ground-truth source, an ambiguous answer cannot be confirmed.
    argo = ArgoConnector()
    with patch.object(argo, "application_exists", AsyncMock(side_effect=PermissionError("denied"))):
        result = await argo.wait_for_application_deletion("app", max_retries=1)
    assert result is False


@pytest.mark.asyncio
async def test_argo_reports_gone_confirmed_by_k8s_is_deleted() -> None:
    argo = ArgoConnector()
    kubectl = _kubectl_returning(False)
    with patch.object(argo, "application_exists", AsyncMock(return_value=False)):
        result = await argo.wait_for_application_deletion("app", max_retries=1, kubectl_connector=kubectl)
    assert result is True
