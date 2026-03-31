"""Unit tests for TaskWorker."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_worker import TaskWorker


@pytest.fixture
def mock_task_service() -> AsyncMock:
    """Fully mocked AsyncTaskService."""
    service = AsyncMock()
    service.claim_next_task = AsyncMock(return_value=None)
    service.start_task = AsyncMock()
    service.send_heartbeat = AsyncMock()
    service.complete_task = AsyncMock()
    service.fail_task = AsyncMock()
    service.recover_stale_tasks = AsyncMock(return_value=0)
    service.cleanup_old_tasks = AsyncMock(return_value=0)
    service.update_progress = AsyncMock()
    service.find_conflicting_task = AsyncMock(return_value=None)
    return service


@pytest.fixture
def mock_worker_settings() -> Any:
    """Mock settings with short intervals for fast tests."""
    with patch("opi.core.task_worker.settings") as s:
        s.TASK_WORKER_POLL_INTERVAL = 0.05
        s.TASK_WORKER_HEARTBEAT_INTERVAL = 0.05
        s.TASK_WORKER_STALE_THRESHOLD = 120
        s.TASK_WORKER_MAX_ATTEMPTS = 3
        s.TASK_WORKER_CLEANUP_RETENTION_HOURS = 72
        s.TASK_WORKER_CONCURRENCY = 1
        s.TASK_WORKER_MAX_DURATION = 1800
        yield s


def _make_task(
    task_id: str = "test-id",
    task_type: str = "upsert_deployment",
    project_name: str = "test",
    payload: dict[str, Any] | None = None,
    attempt_count: int = 0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Helper to build a task dict."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "project_name": project_name,
        "payload": payload or {},
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
    }


@pytest.fixture
def worker(mock_task_service: AsyncMock, mock_worker_settings: Any) -> TaskWorker:
    """Create a TaskWorker with mocked dependencies."""
    return TaskWorker(task_service=mock_task_service, cluster="test-cluster")


async def _run_worker_until(
    worker: TaskWorker,
    condition: asyncio.Event,
    timeout: float = 2.0,
) -> None:
    """Run the worker and cancel it once condition is set or timeout expires."""
    run_task = asyncio.create_task(worker.run())

    async def _wait_and_cancel() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(condition.wait(), timeout=timeout)
        run_task.cancel()

    cancel_task = asyncio.create_task(_wait_and_cancel())

    try:
        await run_task
    except asyncio.CancelledError:
        pass
    finally:
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task


class TestRegisterHandler:
    """Tests for handler registration."""

    def test_register_handler(self, worker: TaskWorker) -> None:
        """Verify handlers can be registered and looked up by task type."""
        handler = AsyncMock()
        worker.register_handler("upsert_deployment", handler)

        assert "upsert_deployment" in worker._handlers
        assert worker._handlers["upsert_deployment"] is handler

    def test_register_multiple_handlers(self, worker: TaskWorker) -> None:
        """Multiple task types can each have their own handler."""
        handler_a = AsyncMock()
        handler_b = AsyncMock()
        worker.register_handler("type_a", handler_a)
        worker.register_handler("type_b", handler_b)

        assert worker._handlers["type_a"] is handler_a
        assert worker._handlers["type_b"] is handler_b


class TestWorkerClaimsAndDispatches:
    """Tests for the core claim-and-dispatch flow."""

    @patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
    async def test_worker_claims_and_dispatches(
        self,
        mock_progress_cls: MagicMock,
        worker: TaskWorker,
        mock_task_service: AsyncMock,
    ) -> None:
        """Worker claims a task and calls the registered handler."""
        task = _make_task()
        mock_task_service.claim_next_task.side_effect = [task, None]

        mock_progress_instance = AsyncMock()
        mock_progress_cls.return_value = mock_progress_instance

        handler = AsyncMock(return_value={"status": "ok"})
        worker.register_handler("upsert_deployment", handler)

        done = asyncio.Event()

        original_complete = mock_task_service.complete_task

        async def complete_and_signal(*args: Any, **kwargs: Any) -> None:
            await original_complete(*args, **kwargs)
            done.set()

        mock_task_service.complete_task = AsyncMock(side_effect=complete_and_signal)

        await _run_worker_until(worker, done)

        # Handler was called with the task payload and progress manager
        handler.assert_called_once_with(
            payload={},
            progress=mock_progress_instance,
        )
        mock_task_service.start_task.assert_called_once_with("test-id")
        mock_task_service.complete_task.assert_called_once_with("test-id", {"status": "ok"})
        mock_progress_instance.close.assert_called_once()


class TestWorkerNoHandler:
    """Tests for tasks with no registered handler."""

    @patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
    async def test_worker_no_handler(
        self,
        mock_progress_cls: MagicMock,
        worker: TaskWorker,
        mock_task_service: AsyncMock,
    ) -> None:
        """Worker fails the task when no handler is registered for the task type."""
        task = _make_task(task_type="unknown_type")
        mock_task_service.claim_next_task.side_effect = [task, None]

        done = asyncio.Event()

        original_fail = mock_task_service.fail_task

        async def fail_and_signal(*args: Any, **kwargs: Any) -> None:
            await original_fail(*args, **kwargs)
            done.set()

        mock_task_service.fail_task = AsyncMock(side_effect=fail_and_signal)

        await _run_worker_until(worker, done)

        mock_task_service.fail_task.assert_called_once()
        call_kwargs = mock_task_service.fail_task.call_args.kwargs
        assert call_kwargs["task_id"] == "test-id"
        assert "No handler registered" in call_kwargs["error_message"]
        # complete_task should NOT have been called
        mock_task_service.complete_task.assert_not_called()


class TestWorkerHandlerException:
    """Tests for handler exceptions."""

    @patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
    async def test_worker_handler_exception(
        self,
        mock_progress_cls: MagicMock,
        worker: TaskWorker,
        mock_task_service: AsyncMock,
    ) -> None:
        """When handler raises, worker calls fail_task and does not crash."""
        task = _make_task()
        mock_task_service.claim_next_task.side_effect = [task, None]

        mock_progress_instance = AsyncMock()
        mock_progress_cls.return_value = mock_progress_instance

        handler = AsyncMock(side_effect=RuntimeError("something broke"))
        worker.register_handler("upsert_deployment", handler)

        done = asyncio.Event()

        original_fail = mock_task_service.fail_task

        async def fail_and_signal(*args: Any, **kwargs: Any) -> None:
            await original_fail(*args, **kwargs)
            done.set()

        mock_task_service.fail_task = AsyncMock(side_effect=fail_and_signal)

        await _run_worker_until(worker, done)

        mock_task_service.fail_task.assert_called_once()
        call_kwargs = mock_task_service.fail_task.call_args.kwargs
        assert call_kwargs["task_id"] == "test-id"
        assert "something broke" in call_kwargs["error_message"]
        mock_task_service.complete_task.assert_not_called()
        # Progress manager should still be closed on failure
        mock_progress_instance.close.assert_called_once()


class TestWorkerStop:
    """Tests for graceful shutdown."""

    async def test_worker_stop(
        self,
        worker: TaskWorker,
        mock_task_service: AsyncMock,
    ) -> None:
        """Worker stops gracefully when stop() is called."""
        # No tasks available, worker just polls
        mock_task_service.claim_next_task.return_value = None

        run_task = asyncio.create_task(worker.run())

        # Let the worker poll at least once
        await asyncio.sleep(0.1)
        assert worker._running is True

        await worker.stop()
        assert worker._running is False

        # Cancel the run task (the background loops have long sleeps)
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


class TestWorkerHeartbeat:
    """Tests for heartbeat during task execution."""

    @patch("opi.core.persistent_task_progress.PersistentTaskProgressManager")
    async def test_worker_heartbeat(
        self,
        mock_progress_cls: MagicMock,
        worker: TaskWorker,
        mock_task_service: AsyncMock,
    ) -> None:
        """Verify heartbeat is sent during task execution."""
        task = _make_task()
        mock_task_service.claim_next_task.side_effect = [task, None]

        mock_progress_instance = AsyncMock()
        mock_progress_cls.return_value = mock_progress_instance

        # Handler that takes long enough for a heartbeat to fire
        async def slow_handler(payload: dict[str, Any], progress: Any) -> dict[str, Any]:
            await asyncio.sleep(0.15)
            return {"done": True}

        worker.register_handler("upsert_deployment", slow_handler)

        done = asyncio.Event()

        original_complete = mock_task_service.complete_task

        async def complete_and_signal(*args: Any, **kwargs: Any) -> None:
            await original_complete(*args, **kwargs)
            done.set()

        mock_task_service.complete_task = AsyncMock(side_effect=complete_and_signal)

        await _run_worker_until(worker, done)

        # With heartbeat interval of 0.05s and handler taking 0.15s,
        # at least one heartbeat should have been sent
        assert mock_task_service.send_heartbeat.call_count >= 1
        mock_task_service.send_heartbeat.assert_any_call("test-id")
