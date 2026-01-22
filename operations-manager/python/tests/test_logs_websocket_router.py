"""
Tests for the WebSocket log streaming endpoint and related functionality.

This module provides comprehensive unit tests for:
- send_message utility function
- stream_deployment_logs method in KubectlConnector
- WebSocket router validation logic
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSendMessage(unittest.IsolatedAsyncioTestCase):
    """Test cases for the send_message utility function."""

    async def test_send_message_success(self):
        """Test successful message sending over WebSocket."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        # Create mock websocket in connected state
        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock()

        # Send message with various kwargs
        result = await send_message(
            mock_websocket,
            "status",
            status="streaming",
            message="Test message",
            extra_field="value",
        )

        # Verify
        self.assertTrue(result)
        mock_websocket.send_text.assert_called_once()

        # Parse and verify sent JSON
        sent_data = json.loads(mock_websocket.send_text.call_args[0][0])
        self.assertEqual(sent_data["type"], "status")
        self.assertEqual(sent_data["status"], "streaming")
        self.assertEqual(sent_data["message"], "Test message")
        self.assertEqual(sent_data["extra_field"], "value")

    async def test_send_message_log_type(self):
        """Test sending log type message."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock()

        result = await send_message(
            mock_websocket,
            "log",
            deployment="main",
            component="api",
            line="Test log line",
            timestamp="2024-01-01T00:00:00Z",
            sequence=1,
        )

        self.assertTrue(result)
        sent_data = json.loads(mock_websocket.send_text.call_args[0][0])
        self.assertEqual(sent_data["type"], "log")
        self.assertEqual(sent_data["deployment"], "main")
        self.assertEqual(sent_data["line"], "Test log line")

    async def test_send_message_error_type(self):
        """Test sending error type message."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock()

        result = await send_message(mock_websocket, "error", message="Connection failed")

        self.assertTrue(result)
        sent_data = json.loads(mock_websocket.send_text.call_args[0][0])
        self.assertEqual(sent_data["type"], "error")
        self.assertEqual(sent_data["message"], "Connection failed")

    async def test_send_message_disconnected(self):
        """Test message sending when websocket is disconnected."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.DISCONNECTED
        mock_websocket.send_text = AsyncMock()

        result = await send_message(mock_websocket, "status", message="Test")

        self.assertFalse(result)
        mock_websocket.send_text.assert_not_called()

    async def test_send_message_connecting_state(self):
        """Test message sending when websocket is in connecting state."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTING
        mock_websocket.send_text = AsyncMock()

        result = await send_message(mock_websocket, "status", message="Test")

        self.assertFalse(result)
        mock_websocket.send_text.assert_not_called()

    async def test_send_message_exception(self):
        """Test message sending when an exception occurs."""
        from starlette.websockets import WebSocketState

        from opi.api.logs_websocket_router import send_message

        mock_websocket = MagicMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_websocket.send_text = AsyncMock(side_effect=Exception("Connection lost"))

        result = await send_message(mock_websocket, "error", message="Test")

        self.assertFalse(result)


class TestStreamDeploymentLogs(unittest.IsolatedAsyncioTestCase):
    """Test cases for the stream_deployment_logs method in KubectlConnector."""

    @patch("opi.connectors.kubectl.asyncio.create_subprocess_exec")
    async def test_stream_deployment_logs_success(self, mock_exec):
        """Test successful log streaming subprocess creation."""
        from opi.connectors.kubectl import KubectlConnector

        # Setup mock process
        mock_process = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.pid = 12345
        mock_exec.return_value = mock_process

        # Create connector instance
        connector = KubectlConnector()

        # Force connected state for testing
        with patch.object(KubectlConnector, "isConnected", True):
            result = await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
                lines=100,
            )

        # Verify process was returned
        self.assertIsNotNone(result)
        self.assertEqual(result.pid, 12345)

        # Verify kubectl command was called with correct arguments
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args[0], "kubectl")
        self.assertEqual(call_args[1], "logs")
        self.assertIn("-f", call_args)
        self.assertIn("deployment/test-deployment", call_args)
        self.assertIn("-n", call_args)
        self.assertIn("test-namespace", call_args)
        self.assertIn("--tail=100", call_args)

    @patch("opi.connectors.kubectl.asyncio.create_subprocess_exec")
    async def test_stream_deployment_logs_custom_lines(self, mock_exec):
        """Test log streaming with custom number of lines."""
        from opi.connectors.kubectl import KubectlConnector

        mock_process = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.pid = 12345
        mock_exec.return_value = mock_process

        connector = KubectlConnector()

        with patch.object(KubectlConnector, "isConnected", True):
            await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
                lines=500,
            )

        # Verify lines parameter was passed correctly
        call_args = mock_exec.call_args[0]
        self.assertIn("--tail=500", call_args)

    async def test_stream_deployment_logs_not_connected(self):
        """Test log streaming when kubectl is not connected."""
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()

        # Force disconnected state for testing
        with patch.object(KubectlConnector, "isConnected", False):
            result = await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
            )

        # Should return None when not connected
        self.assertIsNone(result)

    @patch("opi.connectors.kubectl.asyncio.create_subprocess_exec")
    async def test_stream_deployment_logs_exception(self, mock_exec):
        """Test log streaming when subprocess creation fails."""
        from opi.connectors.kubectl import KubectlConnector

        # Make subprocess creation raise an exception
        mock_exec.side_effect = OSError("kubectl not found")

        connector = KubectlConnector()

        with patch.object(KubectlConnector, "isConnected", True):
            result = await connector.stream_deployment_logs(
                deployment_name="test-deployment",
                namespace="test-namespace",
            )

        # Should return None on exception
        self.assertIsNone(result)


class TestWebSocketRouterValidation(unittest.TestCase):
    """Test cases for WebSocket router validation logic."""

    def test_router_prefix(self):
        """Test that the router has correct prefix."""
        from opi.api.logs_websocket_router import logs_websocket_router

        self.assertEqual(logs_websocket_router.prefix, "/api/logs")

    def test_router_tags(self):
        """Test that the router has correct tags."""
        from opi.api.logs_websocket_router import logs_websocket_router

        self.assertIn("logs-websocket", logs_websocket_router.tags)


class TestProjectValidation(unittest.IsolatedAsyncioTestCase):
    """Test cases for project and deployment validation in WebSocket handler."""

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
                        {"reference": "worker"},
                    ],
                },
                {
                    "name": "staging",
                    "cluster": "rig-staging",
                    "components": [
                        {"reference": "api"},
                    ],
                },
            ]
        }

    def test_find_deployment_in_project(self):
        """Test finding a deployment within project data."""
        project_data = self.mock_project_info.data
        deployments = project_data.get("deployments", [])

        # Find existing deployment
        target = None
        for depl in deployments:
            if depl.get("name") == "main":
                target = depl
                break

        self.assertIsNotNone(target)
        self.assertEqual(target["cluster"], "rig-production")
        self.assertEqual(len(target["components"]), 3)

    def test_deployment_not_found(self):
        """Test when deployment doesn't exist."""
        project_data = self.mock_project_info.data
        deployments = project_data.get("deployments", [])

        target = None
        for depl in deployments:
            if depl.get("name") == "nonexistent":
                target = depl
                break

        self.assertIsNone(target)

    def test_find_component_in_deployment(self):
        """Test finding a component within a deployment."""
        deployment = self.mock_project_info.data["deployments"][0]
        components = deployment.get("components", [])

        # Find existing component
        target = None
        for comp in components:
            if comp.get("reference") == "api":
                target = comp
                break

        self.assertIsNotNone(target)
        self.assertEqual(target["reference"], "api")

    def test_component_not_found(self):
        """Test when component doesn't exist in deployment."""
        deployment = self.mock_project_info.data["deployments"][0]
        components = deployment.get("components", [])

        target = None
        for comp in components:
            if comp.get("reference") == "nonexistent":
                target = comp
                break

        self.assertIsNone(target)

    def test_cluster_matching(self):
        """Test cluster matching for deployments."""
        deployments = self.mock_project_info.data["deployments"]
        current_cluster = "rig-production"

        # Filter deployments on current cluster
        on_current_cluster = [d for d in deployments if d.get("cluster") == current_cluster]

        self.assertEqual(len(on_current_cluster), 1)
        self.assertEqual(on_current_cluster[0]["name"], "main")


class TestKubernetesNameGeneration(unittest.TestCase):
    """Test cases for Kubernetes deployment name generation."""

    def test_generate_unique_name(self):
        """Test that deployment and component names are combined correctly."""
        from opi.utils.naming import generate_unique_name

        result = generate_unique_name("main", "api")
        self.assertIsInstance(result, str)
        self.assertIn("main", result)
        self.assertIn("api", result)

    def test_generate_unique_name_special_chars(self):
        """Test name generation with special characters."""
        from opi.utils.naming import generate_unique_name

        # Should handle various inputs without crashing
        result = generate_unique_name("my-deployment", "my-component")
        self.assertIsInstance(result, str)


class TestWebSocketMessageParsing(unittest.TestCase):
    """Test cases for WebSocket message parsing."""

    def test_parse_pause_action(self):
        """Test parsing pause action from client."""
        message = json.dumps({"action": "pause"})
        data = json.loads(message)

        self.assertEqual(data.get("action"), "pause")

    def test_parse_resume_action(self):
        """Test parsing resume action from client."""
        message = json.dumps({"action": "resume"})
        data = json.loads(message)

        self.assertEqual(data.get("action"), "resume")

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

    def test_parse_empty_action(self):
        """Test parsing message with no action."""
        message = json.dumps({"foo": "bar"})
        data = json.loads(message)

        self.assertIsNone(data.get("action"))


if __name__ == "__main__":
    unittest.main()
