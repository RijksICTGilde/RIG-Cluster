"""
Tests for opi.middleware.authorization module.

Tests get_user, _route_requires_sso, and AuthorizationMiddleware.dispatch.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.middleware.authorization import AuthorizationMiddleware, get_user
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match


class TestGetUser:
    """Tests for get_user helper function."""

    def test_returns_user_from_session(self):
        """Should return user dict when present in session."""
        mock_request = MagicMock(spec=Request)
        mock_request.session = {"user": {"email": "test@example.com", "name": "Test"}}

        result = get_user(mock_request)

        assert result == {"email": "test@example.com", "name": "Test"}

    def test_returns_none_when_no_user_in_session(self):
        """Should return None when session has no user."""
        mock_request = MagicMock(spec=Request)
        mock_request.session = {}

        result = get_user(mock_request)

        assert result is None

    def test_returns_none_when_no_session(self):
        """Should return None when request has no session attribute."""
        mock_request = MagicMock(spec=Request)
        del mock_request.session  # Remove the session attribute

        result = get_user(mock_request)

        assert result is None


class TestAuthorizationMiddlewareDispatch:
    """Tests for AuthorizationMiddleware.dispatch."""

    def _make_middleware(self):
        app = MagicMock()
        return AuthorizationMiddleware(app)

    def _make_request(self, path, session=None):
        mock_request = MagicMock(spec=Request)
        mock_request.url = MagicMock()
        mock_request.url.path = path
        mock_request.method = "GET"
        mock_request.state = MagicMock()
        if session is not None:
            mock_request.session = session
        else:
            mock_request.session = {}
        mock_request.app = MagicMock()
        mock_request.app.router = MagicMock()
        mock_request.app.router.routes = []
        return mock_request

    @pytest.mark.asyncio
    async def test_static_files_always_pass(self):
        """Static file requests should always pass through."""
        middleware = self._make_middleware()
        request = self._make_request("/static/css/style.css")
        expected_response = Response(content="ok")
        call_next = AsyncMock(return_value=expected_response)

        result = await middleware.dispatch(request, call_next)

        assert result is expected_response
        call_next.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_api_routes_skip_sso(self):
        """API routes should skip SSO and set user to None."""
        middleware = self._make_middleware()
        request = self._make_request("/api/v1/projects")
        expected_response = Response(content="ok")
        call_next = AsyncMock(return_value=expected_response)

        result = await middleware.dispatch(request, call_next)

        assert result is expected_response
        assert request.state.user is None

    @pytest.mark.asyncio
    async def test_unauthenticated_user_redirected(self):
        """Should redirect to /auth/login when SSO required and no user."""
        middleware = self._make_middleware()
        request = self._make_request("/dashboard", session={})
        call_next = AsyncMock()

        # Make _route_requires_sso return True
        with patch.object(middleware, "_route_requires_sso", return_value=True):
            result = await middleware.dispatch(request, call_next)

        assert result.status_code == 302
        assert result.headers.get("location") == "/auth/login"

    @pytest.mark.asyncio
    async def test_authenticated_user_passes(self):
        """Should pass through when user is authenticated."""
        middleware = self._make_middleware()
        user = {"email": "test@example.com"}
        request = self._make_request("/dashboard", session={"user": user})
        expected_response = Response(content="ok")
        call_next = AsyncMock(return_value=expected_response)

        mock_user_service = MagicMock()
        mock_user_service.is_email_allowed.return_value = True

        with (
            patch.object(middleware, "_route_requires_sso", return_value=True),
            patch("opi.middleware.authorization.get_user_service", return_value=mock_user_service),
        ):
            result = await middleware.dispatch(request, call_next)

        assert result is expected_response
        assert request.state.user == user

    @pytest.mark.asyncio
    async def test_unauthorized_email_redirected(self):
        """Should redirect to permission-denied when email not in allowlist."""
        middleware = self._make_middleware()
        user = {"email": "blocked@example.com"}
        request = self._make_request("/dashboard", session={"user": user})
        call_next = AsyncMock()

        mock_user_service = MagicMock()
        mock_user_service.is_email_allowed.return_value = False

        with (
            patch.object(middleware, "_route_requires_sso", return_value=True),
            patch("opi.middleware.authorization.get_user_service", return_value=mock_user_service),
        ):
            result = await middleware.dispatch(request, call_next)

        assert result.status_code == 302
        assert result.headers.get("location") == "/permission-denied"


class TestRouteRequiresSso:
    """Tests for _route_requires_sso."""

    def test_returns_true_for_sso_annotated_route(self):
        """Should return True when endpoint has _requires_sso=True."""
        middleware = AuthorizationMiddleware(MagicMock())

        mock_endpoint = MagicMock()
        mock_endpoint._requires_sso = True

        mock_route = MagicMock()
        mock_route.matches.return_value = (Match.FULL, {})
        mock_route.endpoint = mock_endpoint

        mock_request = MagicMock(spec=Request)
        mock_request.url = MagicMock()
        mock_request.url.path = "/dashboard"
        mock_request.method = "GET"
        mock_request.app = MagicMock()
        mock_request.app.router.routes = [mock_route]

        assert middleware._route_requires_sso(mock_request) is True

    def test_returns_false_for_non_sso_route(self):
        """Should return False when endpoint has _requires_sso=False."""
        middleware = AuthorizationMiddleware(MagicMock())

        mock_endpoint = MagicMock()
        mock_endpoint._requires_sso = False

        mock_route = MagicMock()
        mock_route.matches.return_value = (Match.FULL, {})
        mock_route.endpoint = mock_endpoint

        mock_request = MagicMock(spec=Request)
        mock_request.url = MagicMock()
        mock_request.url.path = "/public"
        mock_request.method = "GET"
        mock_request.app = MagicMock()
        mock_request.app.router.routes = [mock_route]

        assert middleware._route_requires_sso(mock_request) is False

    def test_defaults_to_true_for_unmatched_route(self):
        """Should default to True for routes that don't match."""
        middleware = AuthorizationMiddleware(MagicMock())

        mock_request = MagicMock(spec=Request)
        mock_request.url = MagicMock()
        mock_request.url.path = "/unknown"
        mock_request.method = "GET"
        mock_request.app = MagicMock()
        mock_request.app.router.routes = []

        assert middleware._route_requires_sso(mock_request) is True
