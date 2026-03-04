"""Unit tests for AsyncTaskService."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from opi.core.async_task_service import AsyncTaskService, _row_to_dict


@pytest.fixture
def mock_pool() -> MagicMock:
    pool = MagicMock()
    pool.acquire = AsyncMock()
    pool.release = AsyncMock()
    return pool


@pytest.fixture
def mock_connection() -> AsyncMock:
    conn = AsyncMock()
    return conn


@pytest.fixture
def task_service(mock_pool: MagicMock) -> AsyncTaskService:
    return AsyncTaskService(pool=mock_pool, cluster="test-cluster")


def _make_task_row(
    task_id: uuid.UUID | None = None,
    task_type: str = "upsert_deployment",
    project_name: str = "test-project",
    deployment_name: str = "main",
    cluster: str = "test-cluster",
    status: str = "pending",
    payload: dict | None = None,
    created_by: str | None = None,
) -> dict:
    """Build a dict that mimics an asyncpg Record for a task row."""
    if task_id is None:
        task_id = uuid.uuid4()
    return {
        "id": task_id,
        "task_type": task_type,
        "project_name": project_name,
        "deployment_name": deployment_name,
        "cluster": cluster,
        "status": status,
        "payload": payload or {},
        "created_by": created_by,
        "created_at": datetime.now(UTC),
        "claimed_by": None,
        "claimed_at": None,
        "started_at": None,
        "completed_at": None,
        "heartbeat_at": None,
        "error_message": None,
        "current_step": None,
        "progress_percent": 0,
        "attempt_count": 0,
        "max_attempts": 3,
        "result": None,
        "subtasks": None,
        "logs": None,
        "events": None,
        "web_addresses": None,
    }


async def test_create_task(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """create_task inserts a new row and returns it as a dict."""
    mock_pool.acquire.return_value = mock_connection
    # No existing task (dedup returns None)
    mock_connection.fetchrow = AsyncMock()
    row = _make_task_row()
    mock_connection.fetchrow.side_effect = [None, row]

    result = await task_service.create_task(
        task_type="upsert_deployment",
        project_name="test-project",
        deployment_name="main",
        cluster="test-cluster",
        payload={"image": "nginx:latest"},
        created_by="user-1",
    )

    assert result is not None
    assert result["task_id"] == str(row["id"])
    assert result["task_type"] == "upsert_deployment"
    assert result["project_name"] == "test-project"
    assert mock_connection.fetchrow.call_count == 2
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_create_task_dedup(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """When an active task already exists, create_task returns the existing one."""
    mock_pool.acquire.return_value = mock_connection
    existing_row = _make_task_row(status="running")
    mock_connection.fetchrow = AsyncMock(return_value=existing_row)

    result = await task_service.create_task(
        task_type="upsert_deployment",
        project_name="test-project",
        deployment_name="main",
        cluster="test-cluster",
        payload={"image": "nginx:latest"},
    )

    assert result["task_id"] == str(existing_row["id"])
    assert result["status"] == "running"
    # Only the dedup SELECT should have been called, no INSERT
    mock_connection.fetchrow.assert_awaited_once()
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_claim_next_task(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """claim_next_task returns a task and sets claimed_by."""
    mock_pool.acquire.return_value = mock_connection
    task_id = uuid.uuid4()

    # First fetchrow returns the pending task id, second returns the updated row
    select_row = {"id": task_id}
    claimed_row = _make_task_row(
        task_id=task_id,
        status="claimed",
    )
    claimed_row["claimed_by"] = "test-host"
    mock_connection.fetchrow = AsyncMock(side_effect=[select_row, claimed_row])

    # transaction() must return a synchronous object that supports async with
    tx_ctx = MagicMock()
    tx_ctx.__aenter__ = AsyncMock()
    tx_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_connection.transaction = MagicMock(return_value=tx_ctx)

    result = await task_service.claim_next_task(cluster="test-cluster")

    assert result is not None
    assert result["task_id"] == str(task_id)
    assert result["status"] == "claimed"
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_claim_next_task_empty(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """Returns None when no pending tasks are available."""
    mock_pool.acquire.return_value = mock_connection
    mock_connection.fetchrow = AsyncMock(return_value=None)
    tx_ctx = MagicMock()
    tx_ctx.__aenter__ = AsyncMock()
    tx_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_connection.transaction = MagicMock(return_value=tx_ctx)

    result = await task_service.claim_next_task(cluster="test-cluster")

    assert result is None
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_start_task(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """start_task updates status to running."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.start_task(task_id)

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "status = 'running'" in query
    assert "started_at = NOW()" in query
    assert call_args[0][1] == uuid.UUID(task_id)
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_complete_task(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """complete_task sets status to completed, stores result, and sets completed_at."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())
    result_data = {"url": "https://example.com"}

    await task_service.complete_task(task_id, result=result_data)

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "status = 'completed'" in query
    assert "completed_at = NOW()" in query
    assert "progress_percent = 100" in query
    assert call_args[0][1] == uuid.UUID(task_id)
    assert call_args[0][2] == json.dumps(result_data)
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_fail_task_retry(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """When attempt_count < max_attempts, the task goes back to pending for retry."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.fail_task(
        task_id=task_id,
        error_message="Connection timeout",
        attempt_count=1,
        max_attempts=3,
    )

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "status = 'pending'" in query
    assert "claimed_by = NULL" in query
    assert "attempt_count = attempt_count + 1" in query
    assert call_args[0][1] == uuid.UUID(task_id)
    assert call_args[0][2] == "Connection timeout"
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_fail_task_permanent(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """When attempt_count >= max_attempts, the task is marked as permanently failed."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.fail_task(
        task_id=task_id,
        error_message="Fatal error",
        attempt_count=3,
        max_attempts=3,
    )

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "status = 'failed'" in query
    assert "completed_at = NOW()" in query
    assert call_args[0][1] == uuid.UUID(task_id)
    assert call_args[0][2] == "Fatal error"
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_get_task(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """get_task returns a task dict when found."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())
    row = _make_task_row(task_id=uuid.UUID(task_id))
    mock_connection.fetchrow = AsyncMock(return_value=row)

    result = await task_service.get_task(task_id)

    assert result is not None
    assert result["task_id"] == task_id
    assert result["project_name"] == "test-project"
    mock_connection.fetchrow.assert_awaited_once()
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_get_task_not_found(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """get_task returns None when the task does not exist."""
    mock_pool.acquire.return_value = mock_connection
    mock_connection.fetchrow = AsyncMock(return_value=None)

    result = await task_service.get_task(str(uuid.uuid4()))

    assert result is None
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_update_progress(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """update_progress builds a dynamic UPDATE query with only the provided fields."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.update_progress(
        task_id=task_id,
        current_step="Building image",
        progress_percent=42,
    )

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "heartbeat_at = NOW()" in query
    assert "current_step = $2" in query
    assert "progress_percent = $3" in query
    # subtasks, logs, events, web_addresses should NOT appear
    assert "subtasks" not in query
    assert "logs" not in query
    assert "events" not in query
    assert "web_addresses" not in query
    # Verify positional params
    assert call_args[0][1] == uuid.UUID(task_id)
    assert call_args[0][2] == "Building image"
    assert call_args[0][3] == 42
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_update_progress_heartbeat_only(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """When no optional fields are given, only heartbeat_at is updated."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.update_progress(task_id=task_id)

    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "heartbeat_at = NOW()" in query
    assert "current_step" not in query
    assert "progress_percent" not in query
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_recover_stale_tasks(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """recover_stale_tasks re-queues stale tasks and returns the requeued count."""
    mock_pool.acquire.return_value = mock_connection
    # asyncpg execute returns a status string like "UPDATE 2"
    mock_connection.execute = AsyncMock(side_effect=["UPDATE 2", "UPDATE 1"])

    count = await task_service.recover_stale_tasks(stale_threshold_seconds=300)

    assert count == 2
    assert mock_connection.execute.await_count == 2
    # First call: re-queue tasks with retries left
    first_query = mock_connection.execute.call_args_list[0][0][0]
    assert "status = 'pending'" in first_query
    assert "attempt_count < max_attempts" in first_query
    # Second call: mark exhausted tasks as failed
    second_query = mock_connection.execute.call_args_list[1][0][0]
    assert "status = 'failed'" in second_query
    assert "attempt_count >= max_attempts" in second_query
    mock_pool.release.assert_awaited_once_with(mock_connection)


async def test_update_task_status(
    task_service: AsyncTaskService,
    mock_pool: MagicMock,
    mock_connection: AsyncMock,
) -> None:
    """update_task_status updates the status field (e.g. for cancellation)."""
    mock_pool.acquire.return_value = mock_connection
    task_id = str(uuid.uuid4())

    await task_service.update_task_status(task_id, status="cancelled")

    mock_connection.execute.assert_awaited_once()
    call_args = mock_connection.execute.call_args
    query = call_args[0][0]
    assert "status = $2" in query
    assert "completed_at" in query
    assert call_args[0][1] == uuid.UUID(task_id)
    assert call_args[0][2] == "cancelled"
    mock_pool.release.assert_awaited_once_with(mock_connection)


def test_row_to_dict_none() -> None:
    """_row_to_dict returns None for a None input."""
    assert _row_to_dict(None) is None


def test_row_to_dict_serialization() -> None:
    """_row_to_dict converts UUIDs and datetimes, and adds task_id alias."""
    task_id = uuid.uuid4()
    now = datetime.now(UTC)
    row = {"id": task_id, "created_at": now, "name": "test"}

    result = _row_to_dict(row)

    assert result["id"] == str(task_id)
    assert result["task_id"] == str(task_id)
    assert result["created_at"] == now.isoformat()
    assert result["name"] == "test"
