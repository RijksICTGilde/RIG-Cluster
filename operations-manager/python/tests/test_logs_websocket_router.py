"""
Tests for the WebSocket log streaming endpoint and related functionality.

This module provides comprehensive unit tests for:
- Session extraction and authentication
- Authorization checks
- Connection limits and rate limiting
- Log streaming functionality
- Component switching
- Log sanitization
- Session expiration handling
"""

import asyncio
import json
import unittest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

from itsdangerous import BadSignature, SignatureExpired


class TestSendMessage(unittest.IsolatedAsyncioTestCase):
    """Test cases for the send_message utility function."""

    async def test_send_message_success(self):
        """Test successful message sending over WebSocket."""
        from opi.api.logs_websocket_router import send_message
        from starlette.websockets import WebSocketState

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock()

        result = await send_message(
            mock_websocket,
            "status",
            status="streaming",
            message="Test message",
        )

        self.assertTrue(result)
        mock_websocket.send_text.assert_called_once()

        sent_data = json.loads(mock_websocket.send_text.call_args[0][0])
        self.assertEqual(sent_data["type"], "status")
        self.assertEqual(sent_data["status"], "streaming")

    async def test_send_message_disconnected(self):
        """Test message sending when websocket is disconnected."""
        from opi.api.logs_websocket_router import send_message
        from starlette.websockets import WebSocketState

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.DISCONNECTED

        result = await send_message(mock_websocket, "status", message="Test")

        self.assertFalse(result)

    async def test_send_message_exception(self):
        """Test message sending when an exception occurs."""
        from opi.api.logs_websocket_router import send_message
        from starlette.websockets import WebSocketState

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock(side_effect=Exception("Connection lost"))

        result = await send_message(mock_websocket, "error", message="Test")

        self.assertFalse(result)


class TestSessionExtraction(unittest.TestCase):
    """Test cases for session extraction from cookies."""

    @patch("opi.api.logs_websocket_router.settings")
    def test_get_session_no_cookie(self, mock_settings):
        """Test when no session cookie is present."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_websocket = MagicMock()
        mock_websocket.cookies = {}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNone(result)

    @patch("opi.api.logs_websocket_router.settings")
    def test_get_session_no_secret_key(self, mock_settings):
        """Test when SECRET_KEY is not configured."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SECRET_KEY = None
        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "some_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNone(result)

    @patch("opi.api.logs_websocket_router.settings")
    @patch("opi.api.logs_websocket_router.URLSafeTimedSerializer")
    def test_get_session_valid_cookie(self, mock_serializer_class, mock_settings):
        """Test extraction of valid session cookie."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SECRET_KEY = "test_secret"
        mock_serializer = MagicMock()
        mock_serializer.loads.return_value = {"user": {"email": "test@example.com"}}
        mock_serializer_class.return_value = mock_serializer

        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "valid_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNotNone(result)
        self.assertEqual(result["user"]["email"], "test@example.com")

        # Verify max_age and salt are passed
        mock_serializer.loads.assert_called_once()
        call_kwargs = mock_serializer.loads.call_args[1]
        self.assertIn("max_age", call_kwargs)
        self.assertEqual(call_kwargs["salt"], "cookie-session")

    @patch("opi.api.logs_websocket_router.settings")
    @patch("opi.api.logs_websocket_router.URLSafeTimedSerializer")
    def test_get_session_invalid_cookie(self, mock_serializer_class, mock_settings):
        """Test handling of invalid/tampered session cookie (BadSignature)."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SECRET_KEY = "test_secret"
        mock_serializer = MagicMock()
        mock_serializer.loads.side_effect = BadSignature("Invalid signature")
        mock_serializer_class.return_value = mock_serializer

        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "tampered_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNone(result)

    @patch("opi.api.logs_websocket_router.settings")
    @patch("opi.api.logs_websocket_router.URLSafeTimedSerializer")
    def test_get_session_expired_cookie(self, mock_serializer_class, mock_settings):
        """Test handling of expired session cookie (SignatureExpired)."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SECRET_KEY = "test_secret"
        mock_serializer = MagicMock()
        mock_serializer.loads.side_effect = SignatureExpired("Signature has expired")
        mock_serializer_class.return_value = mock_serializer

        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "expired_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNone(result)


class TestUserExtraction(unittest.TestCase):
    """Test cases for user extraction from session."""

    def test_get_user_from_session_valid(self):
        """Test user extraction from valid session."""
        from opi.api.logs_websocket_router import _get_user_from_session

        session = {"user": {"email": "test@example.com", "name": "Test User"}}
        result = _get_user_from_session(session)

        self.assertIsNotNone(result)
        self.assertEqual(result["email"], "test@example.com")

    def test_get_user_from_session_no_user(self):
        """Test user extraction when no user in session."""
        from opi.api.logs_websocket_router import _get_user_from_session

        session = {"other_data": "value"}
        result = _get_user_from_session(session)

        self.assertIsNone(result)

    def test_get_user_from_session_none(self):
        """Test user extraction from None session."""
        from opi.api.logs_websocket_router import _get_user_from_session

        result = _get_user_from_session(None)

        self.assertIsNone(result)


class TestConnectionLimits(unittest.IsolatedAsyncioTestCase):
    """Test cases for connection limit enforcement."""

    def setUp(self):
        """Reset connection tracking before each test."""
        import opi.api.logs_websocket_router as router

        router._active_connections = defaultdict(set)
        router._global_connections = set()

    async def test_register_connection_success(self):
        """Test successful connection registration."""
        from opi.api.logs_websocket_router import _register_connection

        mock_websocket = MagicMock()
        result = await _register_connection("user@example.com", mock_websocket)

        self.assertTrue(result)

    async def test_register_connection_user_limit(self):
        """Test connection rejected when user limit exceeded."""
        from opi.api.logs_websocket_router import (
            MAX_CONNECTIONS_PER_USER,
            _register_connection,
        )

        # Fill up user's connections
        for i in range(MAX_CONNECTIONS_PER_USER):
            mock_ws = MagicMock()
            mock_ws.__hash__ = lambda self, i=i: i
            await _register_connection("user@example.com", mock_ws)

        # Try to add one more
        mock_websocket = MagicMock()
        mock_websocket.__hash__ = lambda self: 999
        result = await _register_connection("user@example.com", mock_websocket)

        self.assertFalse(result)

    async def test_register_connection_global_limit(self):
        """Test connection rejected when global limit exceeded."""
        from opi.api.logs_websocket_router import (
            MAX_GLOBAL_CONNECTIONS,
            _register_connection,
        )

        # Fill up global connections with different users
        for i in range(MAX_GLOBAL_CONNECTIONS):
            mock_ws = MagicMock()
            mock_ws.__hash__ = lambda self, i=i: i
            await _register_connection(f"user{i}@example.com", mock_ws)

        # Try to add one more
        mock_websocket = MagicMock()
        mock_websocket.__hash__ = lambda self: 99999
        result = await _register_connection("newuser@example.com", mock_websocket)

        self.assertFalse(result)

    async def test_unregister_connection(self):
        """Test connection unregistration."""
        from opi.api.logs_websocket_router import (
            _active_connections,
            _global_connections,
            _register_connection,
            _unregister_connection,
        )

        mock_websocket = MagicMock()
        await _register_connection("user@example.com", mock_websocket)

        self.assertEqual(len(_active_connections["user@example.com"]), 1)
        self.assertEqual(len(_global_connections), 1)

        await _unregister_connection("user@example.com", mock_websocket)

        self.assertNotIn("user@example.com", _active_connections)
        self.assertEqual(len(_global_connections), 0)


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Test cases for the rate limiter."""

    async def test_rate_limiter_allows_burst(self):
        """Test that rate limiter allows burst of messages."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=10, burst=5)

        # Should allow burst, acquire returns (allowed, dropped_count)
        for _ in range(5):
            allowed, dropped = limiter.acquire()
            self.assertTrue(allowed)
            self.assertEqual(dropped, 0)  # No dropped messages yet

    async def test_rate_limiter_blocks_after_burst(self):
        """Test that rate limiter blocks after burst is exhausted."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=10, burst=5)

        # Exhaust burst
        for _ in range(5):
            limiter.acquire()

        # Should be blocked
        allowed, dropped = limiter.acquire()
        self.assertFalse(allowed)
        self.assertEqual(dropped, 0)  # Not returned when blocked

    async def test_rate_limiter_refills(self):
        """Test that rate limiter refills over time."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=100, burst=5)  # Fast refill for testing

        # Exhaust burst
        for _ in range(5):
            limiter.acquire()

        # Wait a bit for refill
        await asyncio.sleep(0.1)

        # Should be allowed again
        allowed, _dropped = limiter.acquire()
        self.assertTrue(allowed)

    async def test_rate_limiter_tracks_dropped_messages(self):
        """Test that rate limiter tracks how many messages were dropped."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=10, burst=3)

        # Exhaust burst
        for _ in range(3):
            limiter.acquire()

        # These should be blocked and tracked
        for _ in range(5):
            allowed, _dropped = limiter.acquire()
            self.assertFalse(allowed)

        # Wait for refill
        await asyncio.sleep(0.2)

        # Now we should get the dropped count
        allowed, dropped = limiter.acquire()
        self.assertTrue(allowed)
        self.assertEqual(dropped, 5)  # 5 messages were dropped


class TestLogSanitization(unittest.TestCase):
    """Test cases for log line sanitization."""

    def test_sanitize_normal_log_line(self):
        """Test that normal log lines pass through unchanged."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        line = "2024-01-22 10:30:00 INFO Processing request"
        result = _sanitize_log_line(line)

        self.assertEqual(result, line)

    def test_sanitize_preserves_html_characters(self):
        """Test that HTML characters are preserved (client handles escaping safely)."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        # Server no longer HTML-escapes because client uses textContent (safe)
        # or explicit escapeHtml() for search highlighting
        line = '<script>alert("XSS")</script>'
        result = _sanitize_log_line(line)

        # HTML should be preserved - client handles XSS prevention
        self.assertEqual(result, line)

    def test_sanitize_preserves_html_tags(self):
        """Test that HTML tags are preserved for client-side handling."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        line = 'User input: <img src="x" onerror="alert(1)">'
        result = _sanitize_log_line(line)

        # HTML should be preserved - client uses safe methods
        self.assertEqual(result, line)

    def test_sanitize_control_characters(self):
        """Test that control characters are replaced."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        # Control characters that should be replaced
        line = "Hello\x00World\x07Bell\x1b[31mRed"
        result = _sanitize_log_line(line)

        # Control chars should be replaced with ?
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x07", result)
        self.assertNotIn("\x1b", result)
        self.assertIn("?", result)

    def test_sanitize_preserves_tabs_and_newlines(self):
        """Test that tabs and newlines are preserved."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        line = "Column1\tColumn2\nLine2"
        result = _sanitize_log_line(line)

        self.assertIn("\t", result)
        self.assertIn("\n", result)

    def test_sanitize_long_line_truncation(self):
        """Test that very long lines are truncated."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        # Create a line longer than 10000 characters
        line = "A" * 15000
        result = _sanitize_log_line(line)

        self.assertLess(len(result), 15000)
        self.assertIn("[truncated]", result)

    def test_sanitize_empty_line(self):
        """Test that empty lines are handled."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        result = _sanitize_log_line("")
        self.assertEqual(result, "")

    def test_sanitize_preserves_ampersand(self):
        """Test that ampersands are preserved (client handles escaping)."""
        from opi.api.logs_websocket_router import _sanitize_log_line

        line = "Tom & Jerry"
        result = _sanitize_log_line(line)

        # Ampersand preserved - client uses textContent which is safe
        self.assertEqual(result, "Tom & Jerry")


class TestStreamDeploymentLogs(unittest.IsolatedAsyncioTestCase):
    """Test cases for the stream_deployment_logs method in KubectlConnector."""

    @patch("opi.connectors.kubectl.asyncio.create_subprocess_exec")
    async def test_stream_deployment_logs_success(self, mock_exec):
        """Test successful log streaming subprocess creation."""
        from opi.connectors.kubectl import KubectlConnector

        mock_process = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.pid = 12345
        mock_exec.return_value = mock_process

        connector = KubectlConnector()

        with patch.object(KubectlConnector, "isConnected", True):
            result = await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
                lines=100,
            )

        self.assertIsNotNone(result)
        mock_exec.assert_called_once()

        # Verify stderr is captured (PIPE)
        call_kwargs = mock_exec.call_args[1]
        self.assertEqual(call_kwargs["stderr"], asyncio.subprocess.PIPE)

    async def test_stream_deployment_logs_not_connected(self):
        """Test log streaming when kubectl is not connected."""
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()

        with patch.object(KubectlConnector, "isConnected", False):
            result = await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
            )

        self.assertIsNone(result)


class TestWebSocketRouterValidation(unittest.TestCase):
    """Test cases for WebSocket router validation logic."""

    def test_router_prefix(self):
        """Test that the router has correct prefix."""
        from opi.api.logs_websocket_router import logs_websocket_router

        self.assertEqual(logs_websocket_router.prefix, "/api/logs")


class TestProjectValidation(unittest.TestCase):
    """Test cases for project and deployment validation."""

    def setUp(self):
        """Set up mock project data."""
        self.mock_project_info = MagicMock()
        self.mock_project_info.data = {
            "deployments": [
                {
                    "name": "main",
                    "cluster": "rig-production",
                    "components": [
                        {"reference": "api"},
                        {"reference": "web"},
                    ],
                },
            ]
        }

    def test_find_deployment_in_project(self):
        """Test finding a deployment within project data."""
        project_data = self.mock_project_info.data
        deployments = project_data.get("deployments", [])

        target = None
        for depl in deployments:
            if depl.get("name") == "main":
                target = depl
                break

        self.assertIsNotNone(target)
        self.assertEqual(target["cluster"], "rig-production")

    def test_find_component_in_deployment(self):
        """Test finding a component within a deployment."""
        deployment = self.mock_project_info.data["deployments"][0]
        components = deployment.get("components", [])

        target = None
        for comp in components:
            if comp.get("reference") == "api":
                target = comp
                break

        self.assertIsNotNone(target)


class TestWebSocketMessageParsing(unittest.TestCase):
    """Test cases for WebSocket message parsing."""

    def test_parse_pause_action(self):
        """Test parsing pause action from client."""
        message = json.dumps({"action": "pause"})
        data = json.loads(message)

        self.assertEqual(data.get("action"), "pause")

    def test_parse_switch_action(self):
        """Test parsing switch action from client."""
        message = json.dumps({"action": "switch", "component": "web"})
        data = json.loads(message)

        self.assertEqual(data.get("action"), "switch")
        self.assertEqual(data.get("component"), "web")

    def test_parse_invalid_json(self):
        """Test handling of invalid JSON."""
        message = "not valid json"

        with self.assertRaises(json.JSONDecodeError):
            json.loads(message)


class TestSecurityIntegration(unittest.TestCase):
    """Integration tests for security features."""

    def test_auth_required_before_accept(self):
        """
        Verify that authentication check happens BEFORE websocket.accept().

        This is critical - if we accept first, an attacker can still
        establish a connection even if auth fails.
        """
        # This is a code review test - verify in the source that:
        # 1. _get_session_from_cookie is called
        # 2. User validation happens
        # 3. websocket.accept() is only called after auth passes
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        # Find positions of key operations
        auth_pos = source.find("_get_session_from_cookie")
        accept_pos = source.find("await websocket.accept()")

        self.assertGreater(auth_pos, 0, "Authentication check not found")
        self.assertGreater(accept_pos, 0, "websocket.accept() not found")
        self.assertLess(auth_pos, accept_pos, "Authentication must happen before accept")

    def test_authorization_check_exists(self):
        """Verify that project authorization check is present."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("is_user_authorized_for_project", source, "Project authorization check not found")

    def test_connection_limit_check_exists(self):
        """Verify that connection limit check is present."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("_register_connection", source, "Connection limit check not found")

    def test_generic_error_messages(self):
        """Verify that generic error messages are used to prevent info leakage."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        # Check that we use generic error messages instead of specific ones
        self.assertIn("Access denied", source, "Generic access denied message expected")
        self.assertIn("Resource not found", source, "Generic not found message expected")

        # Verify we don't leak project names in error messages
        self.assertNotIn('f"Project {project_name} not found"', source, "Should not leak project name in error")
        self.assertNotIn('f"User {user_email} not authorized"', source, "Should not leak user email in error")

    def test_rate_limiting_exists(self):
        """Verify that rate limiting is implemented."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("RateLimiter", source, "Rate limiter not found")
        self.assertIn("rate_limiter.acquire", source, "Rate limiter not used")

    def test_log_sanitization_exists(self):
        """Verify that log sanitization is implemented."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("_sanitize_log_line", source, "Log sanitization not found")

    def test_heartbeat_exists(self):
        """Verify that heartbeat mechanism is implemented."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("heartbeat", source, "Heartbeat mechanism not found")

    def test_session_max_age_constant(self):
        """Verify that session max age is defined."""
        from opi.api.logs_websocket_router import SESSION_MAX_AGE_SECONDS

        # Session should expire in reasonable time (24 hours or less)
        self.assertGreater(SESSION_MAX_AGE_SECONDS, 0)
        self.assertLessEqual(SESSION_MAX_AGE_SECONDS, 86400)  # 24 hours

    def test_process_lock_exists(self):
        """Verify that process access is protected by a lock."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("process_lock", source, "Process lock not found")

    def test_csrf_protection_exists(self):
        """Verify that CSRF protection is implemented."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("_validate_origin", source, "CSRF origin validation not found")

    def test_message_size_limit_exists(self):
        """Verify that client message size limit is implemented."""
        import inspect

        from opi.api.logs_websocket_router import stream_logs

        source = inspect.getsource(stream_logs)

        self.assertIn("MAX_CLIENT_MESSAGE_SIZE", source, "Message size limit not found")


class TestCsrfProtection(unittest.TestCase):
    """Test cases for CSRF protection via Origin header validation."""

    def test_validate_origin_no_header(self):
        """Test that requests without Origin header are allowed (same-origin)."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {"host": "example.com"}

        result = _validate_origin(mock_websocket)

        self.assertTrue(result)

    def test_validate_origin_valid_https(self):
        """Test that valid HTTPS origin is allowed."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "host": "example.com",
            "origin": "https://example.com",
        }

        result = _validate_origin(mock_websocket)

        self.assertTrue(result)

    def test_validate_origin_valid_http(self):
        """Test that valid HTTP origin is allowed."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "host": "localhost:8000",
            "origin": "http://localhost:8000",
        }

        result = _validate_origin(mock_websocket)

        self.assertTrue(result)

    def test_validate_origin_invalid_origin(self):
        """Test that invalid origin is rejected."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "host": "example.com",
            "origin": "https://evil.com",
        }

        result = _validate_origin(mock_websocket)

        self.assertFalse(result)

    def test_validate_origin_missing_host(self):
        """Test that request without Host header is rejected."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "origin": "https://example.com",
        }

        result = _validate_origin(mock_websocket)

        self.assertFalse(result)

    def test_validate_origin_different_port(self):
        """Test that different port in origin is rejected."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "host": "example.com:443",
            "origin": "https://example.com:8443",  # Different port
        }

        result = _validate_origin(mock_websocket)

        self.assertFalse(result)

    @patch("opi.api.logs_websocket_router.settings")
    def test_validate_origin_external_url(self, mock_settings):
        """Test that EXTERNAL_URL is included in allowed origins."""
        from opi.api.logs_websocket_router import _validate_origin

        mock_settings.EXTERNAL_URL = "https://public.example.com"

        mock_websocket = MagicMock()
        mock_websocket.headers = {
            "host": "internal.example.com",
            "origin": "https://public.example.com",
        }

        result = _validate_origin(mock_websocket)

        self.assertTrue(result)


class TestClientMessageSizeLimit(unittest.TestCase):
    """Test cases for client message size limit."""

    def test_max_client_message_size_constant(self):
        """Verify that MAX_CLIENT_MESSAGE_SIZE is defined reasonably."""
        from opi.api.logs_websocket_router import MAX_CLIENT_MESSAGE_SIZE

        # Should be positive and reasonable (1-10KB)
        self.assertGreater(MAX_CLIENT_MESSAGE_SIZE, 0)
        self.assertLessEqual(MAX_CLIENT_MESSAGE_SIZE, 10240)  # 10KB max

    def test_normal_message_accepted(self):
        """Test that normal-sized messages are within limits."""
        from opi.api.logs_websocket_router import MAX_CLIENT_MESSAGE_SIZE

        # Typical client message
        message = json.dumps({"action": "pause"})

        self.assertLess(len(message), MAX_CLIENT_MESSAGE_SIZE)

    def test_switch_message_accepted(self):
        """Test that switch messages with component names are within limits."""
        from opi.api.logs_websocket_router import MAX_CLIENT_MESSAGE_SIZE

        # Typical switch message with component name
        message = json.dumps({
            "action": "switch",
            "component": "my-long-component-name-that-is-reasonable",
        })

        self.assertLess(len(message), MAX_CLIENT_MESSAGE_SIZE)

    def test_oversized_message_rejected(self):
        """Test that oversized messages would be rejected."""
        from opi.api.logs_websocket_router import MAX_CLIENT_MESSAGE_SIZE

        # Create a message larger than the limit
        oversized_message = json.dumps({
            "action": "switch",
            "component": "x" * (MAX_CLIENT_MESSAGE_SIZE + 100),
        })

        self.assertGreater(len(oversized_message), MAX_CLIENT_MESSAGE_SIZE)


if __name__ == "__main__":
    unittest.main()
