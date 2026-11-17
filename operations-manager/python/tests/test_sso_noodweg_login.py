"""The break-glass login (``/auth/login/beheer``), past SSO Rijk.

Two things that can break independently:

- without the ``zad-beheer`` OAuth client registered (an unfinished startup), the route
  answers 503 and not a crash;
- the callback shares its session logic with the normal login through ``_finish_login``, so
  an unregistered client there is a failed login and not an unhandled 500.

The guard itself (password, mandatory OTP, the ``platform-beheer`` client role) lives in
Keycloak; see ``features/sso-rijk-uitwijk.md``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from opi.api.auth_routes import beheer_callback, login_beheer

EMAIL = "beheerder@rijksoverheid.nl"


def _request(*, oauth: Any, session: dict | None = None) -> Any:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(oauth=oauth)),
        session=session if session is not None else {},
        url_for=lambda name: f"https://zad.example/auth/{name}",
    )
    return request


async def test_without_the_beheer_client_the_break_glass_login_answers_503() -> None:
    """An unfinished startup may not let the break-glass login crash on an AttributeError."""
    with pytest.raises(HTTPException) as error:
        await login_beheer(_request(oauth=SimpleNamespace()))

    assert error.value.status_code == 503


async def test_the_break_glass_login_redirects_through_the_beheer_callback() -> None:
    beheer = AsyncMock()
    beheer.authorize_redirect = AsyncMock(return_value="redirect")

    answer = await login_beheer(_request(oauth=SimpleNamespace(beheer=beheer)))

    assert answer == "redirect"
    redirect_uri = beheer.authorize_redirect.await_args.args[1]
    assert redirect_uri == "https://zad.example/auth/beheer_callback"


async def test_an_unregistered_client_in_the_callback_is_a_failed_login_not_a_crash() -> None:
    """The client is looked up INSIDE the try; see the docstring of ``_finish_login``."""
    response = await beheer_callback(_request(oauth=SimpleNamespace()))

    assert response.status_code == 302
    assert response.headers["location"].startswith("/?error=")


async def test_the_callback_opens_the_same_kind_of_session_as_the_normal_login() -> None:
    beheer = AsyncMock()
    beheer.authorize_access_token = AsyncMock(
        return_value={"userinfo": {"email": EMAIL, "email_verified": True, "sub": "sub-1"}}
    )
    session: dict = {}

    with patch("opi.api.auth_routes.get_user_service") as get_user_service:
        response = await beheer_callback(_request(oauth=SimpleNamespace(beheer=beheer), session=session))

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    assert session["user"]["email"] == EMAIL
    get_user_service.return_value.store_user.assert_called_once_with(session["user"])


async def test_an_unverified_email_opens_no_session_through_the_break_glass_login() -> None:
    """The same requirement as the normal login: authorization hinges on the email claim."""
    beheer = AsyncMock()
    beheer.authorize_access_token = AsyncMock(
        return_value={"userinfo": {"email": EMAIL, "email_verified": False, "sub": "sub-1"}}
    )
    session: dict = {}

    response = await beheer_callback(_request(oauth=SimpleNamespace(beheer=beheer), session=session))

    assert response.status_code == 302
    assert response.headers["location"] == "/permission-denied"
    assert "user" not in session
