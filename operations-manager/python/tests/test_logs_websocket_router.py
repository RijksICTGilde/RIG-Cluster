"""
Tests for the WebSocket log streaming endpoint and related functionality.

This module provides comprehensive unit tests for:
- Session extraction and authentication
- Authorization checks
- Connection limits and rate limiting
- Log streaming functionality
- Component switching
"""

import asyncio
import json
import unittest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch


class TestSendMessage(unittest.IsolatedAsyncioTestCase):
    """Test cases for the send_message utility function."""

    async def test_send_message_success(self):
        """Test successful message sending over WebSocket."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

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
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.DISCONNECTED

        result = await send_message(mock_websocket, "status", message="Test")

        self.assertFalse(result)

    async def test_send_message_exception(self):
        """Test message sending when an exception occurs."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

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
        """Test when SESSION_SECRET_KEY is not configured."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SESSION_SECRET_KEY = None
        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "some_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNone(result)

    @patch("opi.api.logs_websocket_router.settings")
    @patch("opi.api.logs_websocket_router.URLSafeTimedSerializer")
    def test_get_session_valid_cookie(self, mock_serializer_class, mock_settings):
        """Test extraction of valid session cookie."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SESSION_SECRET_KEY = "test_secret"
        mock_serializer = MagicMock()
        mock_serializer.loads.return_value = {"user": {"email": "test@example.com"}}
        mock_serializer_class.return_value = mock_serializer

        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "valid_cookie"}

        result = _get_session_from_cookie(mock_websocket)

        self.assertIsNotNone(result)
        self.assertEqual(result["user"]["email"], "test@example.com")

    @patch("opi.api.logs_websocket_router.settings")
    @patch("opi.api.logs_websocket_router.URLSafeTimedSerializer")
    def test_get_session_invalid_cookie(self, mock_serializer_class, mock_settings):
        """Test handling of invalid/tampered session cookie."""
        from opi.api.logs_websocket_router import _get_session_from_cookie

        mock_settings.SESSION_SECRET_KEY = "test_secret"
        mock_serializer = MagicMock()
        mock_serializer.loads.side_effect = Exception("Invalid signature")
        mock_serializer_class.return_value = mock_serializer

        mock_websocket = MagicMock()
        mock_websocket.cookies = {"session": "tampered_cookie"}

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


class TestConnectionLimits(unittest.TestCase):
    """Test cases for connection limit enforcement."""

    def setUp(self):
        """Reset connection tracking before each test."""
        import opi.api.logs_websocket_router as router

        router._active_connections = defaultdict(set)
        router._global_connections = set()

    def test_register_connection_success(self):
        """Test successful connection registration."""
        from opi.api.logs_websocket_router import _register_connection

        mock_websocket = MagicMock()
        result = _register_connection("user@example.com", mock_websocket)

        self.assertTrue(result)

    def test_register_connection_user_limit(self):
        """Test connection rejected when user limit exceeded."""
        from opi.api.logs_websocket_router import (
            MAX_CONNECTIONS_PER_USER,
            _register_connection,
        )

        # Fill up user's connections
        for i in range(MAX_CONNECTIONS_PER_USER):
            mock_ws = MagicMock()
            mock_ws.__hash__ = lambda self, i=i: i
            _register_connection("user@example.com", mock_ws)

        # Try to add one more
        mock_websocket = MagicMock()
        mock_websocket.__hash__ = lambda self: 999
        result = _register_connection("user@example.com", mock_websocket)

        self.assertFalse(result)

    def test_register_connection_global_limit(self):
        """Test connection rejected when global limit exceeded."""
        from opi.api.logs_websocket_router import (
            MAX_GLOBAL_CONNECTIONS,
            _register_connection,
        )

        # Fill up global connections with different users
        for i in range(MAX_GLOBAL_CONNECTIONS):
            mock_ws = MagicMock()
            mock_ws.__hash__ = lambda self, i=i: i
            _register_connection(f"user{i}@example.com", mock_ws)

        # Try to add one more
        mock_websocket = MagicMock()
        mock_websocket.__hash__ = lambda self: 99999
        result = _register_connection("newuser@example.com", mock_websocket)

        self.assertFalse(result)

    def test_unregister_connection(self):
        """Test connection unregistration."""
        from opi.api.logs_websocket_router import (
            _active_connections,
            _global_connections,
            _register_connection,
            _unregister_connection,
        )

        mock_websocket = MagicMock()
        _register_connection("user@example.com", mock_websocket)

        self.assertEqual(len(_active_connections["user@example.com"]), 1)
        self.assertEqual(len(_global_connections), 1)

        _unregister_connection("user@example.com", mock_websocket)

        self.assertNotIn("user@example.com", _active_connections)
        self.assertEqual(len(_global_connections), 0)


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Test cases for the rate limiter."""

    async def test_rate_limiter_allows_burst(self):
        """Test that rate limiter allows burst of messages."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=10, burst=5)

        # Should allow burst
        for _ in range(5):
            result = await limiter.acquire()
            self.assertTrue(result)

    async def test_rate_limiter_blocks_after_burst(self):
        """Test that rate limiter blocks after burst is exhausted."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=10, burst=5)

        # Exhaust burst
        for _ in range(5):
            await limiter.acquire()

        # Should be blocked
        result = await limiter.acquire()
        self.assertFalse(result)

    async def test_rate_limiter_refills(self):
        """Test that rate limiter refills over time."""
        from opi.api.logs_websocket_router import RateLimiter

        limiter = RateLimiter(rate=100, burst=5)  # Fast refill for testing

        # Exhaust burst
        for _ in range(5):
            await limiter.acquire()

        # Wait a bit for refill
        await asyncio.sleep(0.1)

        # Should be allowed again
        result = await limiter.acquire()
        self.assertTrue(result)


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
        from opi.api.logs_websocket_router import stream_logs
        import inspect

        source = inspect.getsource(stream_logs)

        # Find positions of key operations
        auth_pos = source.find("_get_session_from_cookie")
        accept_pos = source.find("await websocket.accept()")

        self.assertGreater(auth_pos, 0, "Authentication check not found")
        self.assertGreater(accept_pos, 0, "websocket.accept() not found")
        self.assertLess(auth_pos, accept_pos, "Authentication must happen before accept")

    def test_authorization_check_exists(self):
        """Verify that project authorization check is present."""
        from opi.api.logs_websocket_router import stream_logs
        import inspect

        source = inspect.getsource(stream_logs)

        self.assertIn("is_user_authorized_for_project", source,
                      "Project authorization check not found")

    def test_connection_limit_check_exists(self):
        """Verify that connection limit check is present."""
        from opi.api.logs_websocket_router import stream_logs
        import inspect

        source = inspect.getsource(stream_logs)

        self.assertIn("_register_connection", source,
                      "Connection limit check not found")


if __name__ == "__main__":
    unittest.main()
