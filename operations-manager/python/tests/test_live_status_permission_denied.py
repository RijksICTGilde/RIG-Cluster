"""Regression: a status query for a just-created deployment must not leak ArgoCD's
transient 403 (AppProject RBAC still propagating right after creation).

_fetch_one_live_status should report Pending ("no Application yet / not reconciled")
instead of raising PermissionError to the caller - it self-heals within a minute or two.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from opi.api.v2.models import DeploymentStatus
from opi.api.v2.router import _fetch_one_live_status


@pytest.mark.asyncio
async def test_permission_denied_reports_pending_not_error() -> None:
    argo = Mock()
    argo.get_application_status = AsyncMock(
        side_effect=PermissionError("Permission denied accessing application 'x-main'")
    )
    deployment = {"name": "main", "namespace": "rig-prd-x", "cluster": "odcn-production"}
    result = await _fetch_one_live_status(
        project_name="x",
        project_data={"name": "x", "deployments": [deployment]},
        deployment=deployment,
        argo=argo,
        kubectl=Mock(),
    )
    assert result.status == DeploymentStatus.Pending
    assert result.errors == []
