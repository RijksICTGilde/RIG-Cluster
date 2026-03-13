"""Tests for the federation routing layer."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.opi import OpiConnector, OpiConnectorError
from opi.core.federation_config import FederationRegistry, PeerConfig
from opi.core.federation_service import FederationError, FederationService

# ---------------------------------------------------------------------------
# FederationRegistry tests
# ---------------------------------------------------------------------------


class TestPeerConfig:
    def test_strip_trailing_slash(self):
        peer = PeerConfig(cluster="remote", url="https://slave.example.com/", api_key="key")
        assert peer.url == "https://slave.example.com"

    def test_reject_empty_cluster(self):
        with pytest.raises(ValueError, match="cluster must not be empty"):
            PeerConfig(cluster="  ", url="https://slave.example.com", api_key="key")

    def test_defaults(self):
        peer = PeerConfig(cluster="remote", url="https://slave.example.com", api_key="key")
        assert peer.verify_tls is True
        assert peer.timeout == 30


class TestFederationRegistry:
    def test_from_settings_empty_string(self):
        registry = FederationRegistry.from_settings("", "local")
        assert registry.is_enabled() is False
        assert registry.get_all_peers() == []

    def test_from_settings_valid_json(self):
        peers_json = json.dumps(
            [
                {"cluster": "sandbox", "url": "https://sandbox.opi.local", "api_key": "abc"},
                {"cluster": "prod", "url": "https://prod.opi.local", "api_key": "xyz"},
            ]
        )
        registry = FederationRegistry.from_settings(peers_json, "local")
        assert registry.is_enabled() is True
        assert len(registry.get_all_peers()) == 2

    def test_get_peer(self):
        peers_json = json.dumps(
            [
                {"cluster": "sandbox", "url": "https://sandbox.opi.local", "api_key": "abc"},
            ]
        )
        registry = FederationRegistry.from_settings(peers_json, "local")
        peer = registry.get_peer("sandbox")
        assert peer is not None
        assert peer.cluster == "sandbox"
        assert registry.get_peer("nonexistent") is None

    def test_is_local_cluster(self):
        registry = FederationRegistry.from_settings("[]", "local")
        assert registry.is_local_cluster("local") is True
        assert registry.is_local_cluster("remote") is False

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid FEDERATION_PEERS JSON"):
            FederationRegistry.from_settings("{not valid json", "local")

    def test_non_array_json_raises(self):
        with pytest.raises(TypeError, match="must be a JSON array"):
            FederationRegistry.from_settings('{"key": "value"}', "local")

    def test_url_slash_stripping(self):
        peers_json = json.dumps(
            [
                {"cluster": "remote", "url": "https://remote.opi.local///", "api_key": "k"},
            ]
        )
        registry = FederationRegistry.from_settings(peers_json, "local")
        peer = registry.get_peer("remote")
        assert peer.url == "https://remote.opi.local"


# ---------------------------------------------------------------------------
# OpiConnector tests
# ---------------------------------------------------------------------------


class TestOpiConnector:
    @pytest.mark.asyncio
    async def test_create_task_success(self):
        """Mock aiohttp session and test create_task with 202 response."""
        connector = OpiConnector(
            base_url="https://slave.example.com",
            api_key="test-key",
        )

        mock_response = AsyncMock()
        mock_response.status = 202
        mock_response.json = AsyncMock(
            return_value={
                "status": "accepted",
                "task_id": "abc-123",
                "task_type": "upsert_deployment",
                "poll_url": "/api/tasks/abc-123",
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)

        connector.session = mock_session

        result = await connector.create_task(
            task_type="upsert_deployment",
            project_name="test-project",
            deployment_name="staging",
            cluster="sandbox",
            payload={"key": "value"},
        )

        assert result["task_id"] == "abc-123"
        assert result["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_create_task_non_202_raises(self):
        """Non-202 response should raise OpiConnectorError."""
        connector = OpiConnector(
            base_url="https://slave.example.com",
            api_key="test-key",
        )

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.json = AsyncMock(return_value={"detail": "Internal error"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)

        connector.session = mock_session

        with pytest.raises(OpiConnectorError) as exc_info:
            await connector.create_task(
                task_type="upsert_deployment",
                project_name="test-project",
                deployment_name="staging",
                cluster="sandbox",
                payload={},
            )
        assert exc_info.value.status == 500

    @pytest.mark.asyncio
    async def test_get_task_status_accepts_200_and_202(self):
        """get_task_status should accept both 200 (done) and 202 (in-progress)."""
        connector = OpiConnector(
            base_url="https://slave.example.com",
            api_key="test-key",
        )

        for status_code in (200, 202):
            mock_response = AsyncMock()
            mock_response.status = status_code
            mock_response.json = AsyncMock(
                return_value={
                    "task_id": "abc-123",
                    "status": "completed" if status_code == 200 else "running",
                }
            )
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.get = MagicMock(return_value=mock_response)

            connector.session = mock_session

            result = await connector.get_task_status("abc-123")
            assert result["task_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_headers(self):
        connector = OpiConnector(
            base_url="https://slave.example.com",
            api_key="my-secret-key",
        )
        headers = connector._headers()
        assert headers["X-API-Key"] == "my-secret-key"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_not_initialized_raises(self):
        connector = OpiConnector(
            base_url="https://slave.example.com",
            api_key="test-key",
        )
        with pytest.raises(RuntimeError, match="session not initialized"):
            await connector.health_check()


# ---------------------------------------------------------------------------
# FederationService tests
# ---------------------------------------------------------------------------


class TestFederationService:
    def _make_registry(self, local_cluster="local"):
        peers_json = json.dumps(
            [
                {"cluster": "sandbox", "url": "https://sandbox.opi.local", "api_key": "sandbox-key"},
            ]
        )
        return FederationRegistry.from_settings(peers_json, local_cluster)

    def _make_task_service(self):
        ts = AsyncMock()
        ts.create_task = AsyncMock(return_value={"task_id": "local-task-id", "status": "pending"})
        ts.get_task = AsyncMock(return_value=None)
        ts.update_task_status = AsyncMock()
        return ts

    @pytest.mark.asyncio
    async def test_local_task_delegates_to_task_service(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        service = FederationService(registry, task_service)

        result = await service.create_task(
            task_type="upsert_deployment",
            project_name="proj",
            deployment_name="staging",
            target_cluster="local",
            payload={"key": "value"},
        )

        assert result["task_id"] == "local-task-id"
        task_service.create_task.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remote_task_forwards_to_slave(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        service = FederationService(registry, task_service)

        mock_result = {"task_id": "remote-task-id", "status": "accepted", "poll_url": "/api/tasks/remote-task-id"}

        with patch("opi.core.federation_service.OpiConnector") as MockConnector:
            mock_conn = AsyncMock()
            mock_conn.create_task = AsyncMock(return_value=mock_result)
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            MockConnector.return_value = mock_conn

            result = await service.create_task(
                task_type="upsert_deployment",
                project_name="proj",
                deployment_name="staging",
                target_cluster="sandbox",
                payload={"key": "value"},
            )

        assert result["task_id"] == "remote-task-id"
        assert "remote-task-id" in service._route_table
        task_service.create_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_cluster_raises(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        service = FederationService(registry, task_service)

        with pytest.raises(FederationError, match="Unknown target cluster"):
            await service.create_task(
                task_type="upsert_deployment",
                project_name="proj",
                deployment_name="staging",
                target_cluster="nonexistent",
                payload={},
            )

    @pytest.mark.asyncio
    async def test_get_task_status_returns_local_first(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        task_service.get_task = AsyncMock(return_value={"task_id": "local-123", "status": "running"})
        service = FederationService(registry, task_service)

        result = await service.get_task_status("local-123")
        assert result["task_id"] == "local-123"
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_task_status_proxies_via_route_table(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        task_service.get_task = AsyncMock(return_value=None)
        service = FederationService(registry, task_service)

        # Manually add route table entry
        service._route_table["remote-123"] = "https://sandbox.opi.local"

        mock_result = {"task_id": "remote-123", "status": "running", "progress_percent": 50}

        with patch("opi.core.federation_service.OpiConnector") as MockConnector:
            mock_conn = AsyncMock()
            mock_conn.get_task_status = AsyncMock(return_value=mock_result)
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            MockConnector.return_value = mock_conn

            result = await service.get_task_status("remote-123")

        assert result["task_id"] == "remote-123"
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_task_status_returns_none_when_not_found(self):
        registry = self._make_registry(local_cluster="local")
        task_service = self._make_task_service()
        task_service.get_task = AsyncMock(return_value=None)
        service = FederationService(registry, task_service)

        result = await service.get_task_status("unknown-id")
        assert result is None


# ---------------------------------------------------------------------------
# task_router proxy tests
# ---------------------------------------------------------------------------


class TestTaskRouterFederationProxy:
    """Test that GET /api/tasks/{id} proxies through federation_service."""

    @pytest.mark.asyncio
    async def test_get_task_proxies_via_federation(self):
        """When local task_service returns None, federation_service is consulted."""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        app = FastAPI()

        # Import and register the task router
        from opi.api.task_router import task_router

        app.include_router(task_router)

        # Mock task_service (returns None for local lookup)
        mock_task_service = AsyncMock()
        mock_task_service.get_task = AsyncMock(return_value=None)
        app.state.task_service = mock_task_service

        # Mock federation_service (returns a remote task with project_name)
        mock_fed_service = AsyncMock()
        mock_fed_service.get_task_status = AsyncMock(
            return_value={
                "task_id": "fed-task-123",
                "task_type": "upsert_deployment",
                "status": "running",
                "progress_percent": 42,
                "current_step": "deploying",
                "created_at": "2026-03-01T10:00:00",
                "project_name": "test-project",
            }
        )
        app.state.federation_service = mock_fed_service

        # Mock project service for API key validation
        mock_project = MagicMock()
        mock_project.api_key = "test-api-key"
        mock_project.name = "test-project"
        mock_project_service = MagicMock()
        mock_project_service.get_project.return_value = mock_project

        transport = ASGITransport(app=app)
        with patch("opi.api.task_router.get_project_service", return_value=mock_project_service):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Use a valid UUID with API key
                resp = await client.get(
                    "/api/tasks/00000000-0000-0000-0000-000000000001",
                    headers={"X-API-Key": "test-api-key"},
                )

        assert resp.status_code == 202
        body = resp.json()
        assert body["task_id"] == "fed-task-123"
        assert body["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_task_returns_404_when_not_found_anywhere(self):
        """When both local and federation return None, should 404."""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        app = FastAPI()

        from opi.api.task_router import task_router

        app.include_router(task_router)

        mock_task_service = AsyncMock()
        mock_task_service.get_task = AsyncMock(return_value=None)
        app.state.task_service = mock_task_service

        mock_fed_service = AsyncMock()
        mock_fed_service.get_task_status = AsyncMock(return_value=None)
        app.state.federation_service = mock_fed_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/00000000-0000-0000-0000-000000000002")

        assert resp.status_code == 404
