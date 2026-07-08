"""Regression: an unavailable subdomain must surface as a FieldError tied to the
subdomain input (so it actually renders), and name the project that holds it.

Previously the availability check raised a plain ValueError, which the form
processor keyed to the deployment-group path (deployments[N]) with no visible
input — so the message never appeared and the wizard just re-rendered.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldError


@pytest.mark.asyncio
async def test_taken_subdomain_raises_fielderror_on_field_with_owner():
    conn = MagicMock()
    conn.get_by_subdomain = AsyncMock(return_value={"project_name": "mozad-dle"})
    with (
        patch("opi.forms.editables.enforcers.SubdomainConnector", return_value=conn),
        pytest.raises(FieldError) as exc_info,
    ):
        await DomainConfigEnforcer._check_subdomain_availability(
            "moza",
            "rijksapp.dev",
            {"project_name": "nd-j7s"},
            field_path="deployments[1]/subdomain",
        )

    assert exc_info.value.field_path == "deployments[1]/subdomain"
    assert "mozad-dle" in str(exc_info.value)


@pytest.mark.asyncio
async def test_own_subdomain_is_allowed():
    conn = MagicMock()
    conn.get_by_subdomain = AsyncMock(return_value={"project_name": "nd-j7s"})
    with patch("opi.forms.editables.enforcers.SubdomainConnector", return_value=conn):
        # Same project already owns it (edit mode) -> no error.
        await DomainConfigEnforcer._check_subdomain_availability(
            "moza",
            "rijksapp.dev",
            {"project_name": "nd-j7s"},
            field_path="deployments[1]/subdomain",
        )


@pytest.mark.asyncio
async def test_free_subdomain_is_allowed():
    conn = MagicMock()
    conn.get_by_subdomain = AsyncMock(return_value=None)
    with patch("opi.forms.editables.enforcers.SubdomainConnector", return_value=conn):
        await DomainConfigEnforcer._check_subdomain_availability(
            "vrij",
            "rijksapp.dev",
            {"project_name": "nd-j7s"},
            field_path="deployments[1]/subdomain",
        )
