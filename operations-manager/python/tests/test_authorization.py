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
    async def test_api_routes_skip_sso_by_default(self):
        """API routes without @requires_sso should skip SSO and set user to None."""
        middleware = self._make_middleware()
        request = self._make_request("/api/v1/projects")
        expected_response = Response(content="ok")
        call_next = AsyncMock(return_value=expected_response)

        with patch.object(middleware, "_route_requires_sso", return_value=False):
            result = await middleware.dispatch(request, call_next)

        assert result is expected_response
        assert request.state.user is None

    @pytest.mark.asyncio
    async def test_metrics_scrape_endpoint_skips_auth(self):
        """The exact /metrics Prometheus scrape endpoint must remain public."""
        middleware = self._make_middleware()
        request = self._make_request("/metrics")
        expected_response = Response(content="ok")
        call_next = AsyncMock(return_value=expected_response)

        result = await middleware.dispatch(request, call_next)

        assert result is expected_response
        call_next.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_metrics_explorer_is_not_skipped_by_metrics_prefix(self):
        """Regression: /metrics-explorer must NOT be treated as public by the /metrics rule."""
        middleware = self._make_middleware()
        request = self._make_request("/metrics-explorer", session={})
        call_next = AsyncMock()

        with patch.object(middleware, "_route_requires_sso", return_value=True):
            result = await middleware.dispatch(request, call_next)

        assert result.status_code == 302
        assert result.headers.get("location") == "/auth/login"
        call_next.assert_not_awaited()

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

    def test_unmatched_route_log_matches_return_value(self):
        """Log message must agree with actual return value."""
        import inspect

        source = inspect.getsource(AuthorizationMiddleware._route_requires_sso)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "Could not determine SSO requirement" in stripped:
                # Find the return statement that follows
                for j in range(i + 1, len(lines)):
                    next_stripped = lines[j].strip()
                    if next_stripped.startswith("return"):
                        returns_true = "True" in next_stripped
                        break
                # If the function returns True (requiring SSO), the log should NOT say "NOT require"
                if returns_true:
                    assert "NOT require SSO" not in stripped, (
                        f"Log says 'NOT require SSO' but function returns True (requires SSO): {stripped}"
                    )

    def test_defaults_to_false_for_unannotated_endpoint(self):
        """An endpoint WITHOUT _requires_sso attribute should default to False (opt-in SSO)."""
        middleware = AuthorizationMiddleware(MagicMock())

        # Endpoint that does NOT have _requires_sso attribute at all
        mock_endpoint = lambda: None  # noqa: E731 - plain function, no _requires_sso attr

        mock_route = MagicMock()
        mock_route.matches.return_value = (Match.FULL, {})
        mock_route.endpoint = mock_endpoint

        mock_request = MagicMock(spec=Request)
        mock_request.url = MagicMock()
        mock_request.url.path = "/some-route"
        mock_request.method = "GET"
        mock_request.app = MagicMock()
        mock_request.app.router.routes = [mock_route]

        result = middleware._route_requires_sso(mock_request)
        assert result is False, (
            f"Matched endpoints without _requires_sso should default to False (opt-in), got {result}"
        )

    def test_default_sso_getattr_uses_false(self):
        """The getattr default for _requires_sso should be False (opt-in SSO via decorator)."""
        import inspect

        source = inspect.getsource(AuthorizationMiddleware._route_requires_sso)
        for line in source.split("\n"):
            stripped = line.strip()
            if "_requires_sso" in stripped and "getattr" in stripped:
                assert "False)" in stripped, f"getattr default for _requires_sso should be False: {stripped}"
