"""Where a failing Keycloak token exchange leaves the person on the invite page."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from opi.api.invite_routes import invite_sso_callback

PROJECT = ("demo", {"display-name": "Demo-applicatie"}, {"contact_email": "beheer@example.org"}, "sandbox")


def _request() -> MagicMock:
    request = MagicMock()
    request.session = {
        "invite_flow": {
            "key": "sleutel",
            "state": "s1",
            "keycloak_url": "https://kc.example.org",
            "realm_name": "rig-demo",
            "code_verifier": "v",
            "redirect_uri": "https://x/cb",
        }
    }
    request.query_params = {"state": "s1", "code": "c1"}
    request.headers = {}
    return request


async def _callback_with_exchange_failure(error: Exception) -> Any:
    manager = MagicMock()
    manager.validate_auth_method.return_value = True
    with (
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
        patch("opi.api.invite_routes._exchange_code_for_token", AsyncMock(side_effect=error)),
    ):
        return await invite_sso_callback(_request(), "sleutel")


async def test_a_refused_token_exchange_shows_the_token_error() -> None:
    error = httpx.HTTPStatusError("400", request=httpx.Request("POST", "https://kc"), response=httpx.Response(400))

    response = await _callback_with_exchange_failure(error)

    assert response.headers["location"] == "/invite/sleutel/error?code=token_error"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route to host"),
        httpx.ReadTimeout("too slow"),
    ],
)
async def test_an_unreachable_keycloak_also_shows_the_token_error(error: Exception) -> None:
    """A transport failure in the exchange is a token error, not an unknown one."""
    response = await _callback_with_exchange_failure(error)

    assert response.headers["location"] == "/invite/sleutel/error?code=token_error"
