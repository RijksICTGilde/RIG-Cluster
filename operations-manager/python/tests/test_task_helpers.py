"""
Unit tests for opi.core.task_helpers.

Tests cover:
- create_async_task: local and federation routing
- wait_for_task_completion: polling, timeout, terminal states
- build_accepted_response: response structure
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.task_helpers import (
    build_accepted_response,
    create_async_task,
    get_task_service,
    wait_for_task_completion,
)

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"


def _make_request(
    task_service: Any = None,
    federation_service: Any = None,
) -> MagicMock:
    """Build a mock FastAPI Request with app.state."""
    request = MagicMock()
    request.app.state = MagicMock()

    if task_service is not None:
        request.app.state.task_service = task_service
    else:
        # Make getattr return None for task_service when not set
        del request.app.state.task_service

    if federation_service is not None:
        request.app.state.federation_service = federation_service
    else:
        del request.app.state.federation_service

    return request


# ---------------------------------------------------------------------------
# get_task_service
# ---------------------------------------------------------------------------


class TestGetTaskService:
    def test_returns_service_when_available(self) -> None:
        mock_service = AsyncMock()
        request = MagicMock()
        request.app.state.task_service = mock_service

        result = get_task_service(request)
        assert result is mock_service

    def test_raises_503_when_unavailable(self) -> None:
        request = MagicMock()
        request.app.state = MagicMock(spec=[])  # no attributes

        with pytest.raises(HTTPException) as exc_info:
            get_task_service(request)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# build_accepted_response
# ---------------------------------------------------------------------------


class TestBuildAcceptedResponse:
    def test_structure(self) -> None:
        result = build_accepted_response("abc-123", "upsert_deployment")
        assert result == {
            "status": "accepted",
            "task_id": "abc-123",
            "task_type": "upsert_deployment",
            "poll_url": "/api/tasks/abc-123",
        }

    def test_different_task_types(self) -> None:
        for task_type in [
            "upsert_deployment",
            "update_image",
            "delete_deployment",
            "clone_database",
            "clone_bucket",
            "refresh_deployment",
            "create_project",
        ]:
            result = build_accepted_response(SAMPLE_TASK_ID, task_type)
            assert result["task_type"] == task_type
            assert SAMPLE_TASK_ID in result["poll_url"]


# ---------------------------------------------------------------------------
# create_async_task
# ---------------------------------------------------------------------------


class TestCreateAsyncTask:
    @pytest.mark.asyncio
    async def test_creates_task_locally(self) -> None:
        mock_service = AsyncMock()
        mock_service.create_task.return_value = {"task_id": SAMPLE_TASK_ID}

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        with patch("opi.core.task_helpers.settings") as mock_settings:
            mock_settings.CLUSTER_MANAGER = "local"
            result = await create_async_task(
                request=request,
                task_type="upsert_deployment",
                project_name="my-project",
                deployment_name="main",
                payload={"key": "value"},
            )

        assert result["task_id"] == SAMPLE_TASK_ID
        mock_service.create_task.assert_awaited_once_with(
            task_type="upsert_deployment",
            project_name="my-project",
            deployment_name="main",
            cluster="local",
            payload={"key": "value"},
            max_attempts=None,
        )

    @pytest.mark.asyncio
    async def test_routes_through_federation(self) -> None:
        mock_service = AsyncMock()
        mock_federation = AsyncMock()
        mock_federation.resolve_cluster.return_value = "cluster-b"
        mock_federation.create_task.return_value = {"task_id": SAMPLE_TASK_ID}

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = mock_federation

        result = await create_async_task(
            request=request,
            task_type="upsert_deployment",
            project_name="my-project",
            deployment_name="staging",
            payload={"foo": "bar"},
        )

        assert result["task_id"] == SAMPLE_TASK_ID
        mock_federation.resolve_cluster.assert_awaited_once_with("my-project", "staging")
        mock_federation.create_task.assert_awaited_once_with(
            task_type="upsert_deployment",
            project_name="my-project",
            deployment_name="staging",
            target_cluster="cluster-b",
            payload={"foo": "bar"},
        )
        # Local task service should NOT be called
        mock_service.create_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_503_when_no_task_service(self) -> None:
        request = MagicMock()
        request.app.state = MagicMock(spec=[])

        with pytest.raises(HTTPException) as exc_info:
            await create_async_task(
                request=request,
                task_type="upsert_deployment",
                project_name="my-project",
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_default_payload_is_empty_dict(self) -> None:
        mock_service = AsyncMock()
        mock_service.create_task.return_value = {"task_id": SAMPLE_TASK_ID}

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        with patch("opi.core.task_helpers.settings") as mock_settings:
            mock_settings.CLUSTER_MANAGER = "local"
            await create_async_task(
                request=request,
                task_type="create_project",
                project_name="test",
            )

        call_kwargs = mock_service.create_task.call_args[1]
        assert call_kwargs["payload"] == {}
        assert call_kwargs["deployment_name"] is None


# ---------------------------------------------------------------------------
# wait_for_task_completion
# ---------------------------------------------------------------------------


class TestWaitForTaskCompletion:
    @pytest.mark.asyncio
    async def test_returns_on_completed(self) -> None:
        mock_service = AsyncMock()
        mock_service.get_task.return_value = {
            "task_id": SAMPLE_TASK_ID,
            "status": "completed",
            "result": {"deployment_name": "main"},
        }

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        result = await wait_for_task_completion(
            request=request,
            task_id=SAMPLE_TASK_ID,
        )

        assert result["status"] == "completed"
        assert result["result"]["deployment_name"] == "main"

    @pytest.mark.asyncio
    async def test_returns_on_failed(self) -> None:
        mock_service = AsyncMock()
        mock_service.get_task.return_value = {
            "task_id": SAMPLE_TASK_ID,
            "status": "failed",
            "error_message": "Something went wrong",
        }

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        result = await wait_for_task_completion(
            request=request,
            task_id=SAMPLE_TASK_ID,
        )

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_returns_on_cancelled(self) -> None:
        mock_service = AsyncMock()
        mock_service.get_task.return_value = {
            "task_id": SAMPLE_TASK_ID,
            "status": "cancelled",
        }

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        result = await wait_for_task_completion(
            request=request,
            task_id=SAMPLE_TASK_ID,
        )

        assert result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_polls_until_completion(self) -> None:
        """Should keep polling while status is running, then return when completed."""
        mock_service = AsyncMock()
        call_count = 0

        async def get_task_side_effect(task_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return {"task_id": task_id, "status": "running"}
            return {
                "task_id": task_id,
                "status": "completed",
                "result": {"done": True},
            }

        mock_service.get_task.side_effect = get_task_side_effect

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        result = await wait_for_task_completion(
            request=request,
            task_id=SAMPLE_TASK_ID,
            poll_interval=0.01,  # fast polling for test
        )

        assert result["status"] == "completed"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self) -> None:
        """Should raise TimeoutError if task doesn't complete in time."""
        mock_service = AsyncMock()
        mock_service.get_task.return_value = {
            "task_id": SAMPLE_TASK_ID,
            "status": "running",
        }

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        with pytest.raises(TimeoutError, match="did not complete"):
            await wait_for_task_completion(
                request=request,
                task_id=SAMPLE_TASK_ID,
                timeout_seconds=0.05,
                poll_interval=0.01,
            )

    @pytest.mark.asyncio
    async def test_task_not_found_raises_404(self) -> None:
        mock_service = AsyncMock()
        mock_service.get_task.return_value = None

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = None

        with pytest.raises(HTTPException) as exc_info:
            await wait_for_task_completion(
                request=request,
                task_id=SAMPLE_TASK_ID,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_tries_federation_when_local_not_found(self) -> None:
        """If task not found locally, should try federation service."""
        mock_service = AsyncMock()
        mock_service.get_task.return_value = None

        mock_federation = AsyncMock()
        mock_federation.get_task_status.return_value = {
            "task_id": SAMPLE_TASK_ID,
            "status": "completed",
            "result": {"from": "federation"},
        }

        request = MagicMock()
        request.app.state.task_service = mock_service
        request.app.state.federation_service = mock_federation

        result = await wait_for_task_completion(
            request=request,
            task_id=SAMPLE_TASK_ID,
        )

        assert result["status"] == "completed"
        assert result["result"]["from"] == "federation"
