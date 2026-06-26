"""Real-Postgres integration tests for per-project claim serialization (1a).

Proves the actual SQL semantics of AsyncTaskService.claim_next_task against a
live database, not just the query string. Reproduces the toets-hn7/pr-36
scenario: two sibling deployment deletes of the same project must NOT both be
claimed, while unrelated work (other projects, non-mutating task types) must
still be claimable.

Run against an ephemeral Postgres:

    docker run -d --rm -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
    TEST_DATABASE_DSN=postgresql://postgres:pw@localhost:55432/postgres \\
        uv run pytest tests/test_async_task_claim_serialization_db.py -m requires_infra -q
"""

import os
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from opi.core.async_task_schema import ASYNC_TASKS_TABLE_SQL
from opi.core.async_task_service import AsyncTaskService

pytestmark = [pytest.mark.requires_infra]

CLUSTER = "odcn-production"
DSN = os.environ.get("TEST_DATABASE_DSN")


# asyncpg.Pool is duck-compatible with the DatabasePool interface AsyncTaskService
# uses (acquire/release + connection transaction/fetchrow/execute); typed as Any
# so the real pool can be passed directly.
@pytest.fixture
async def pool() -> AsyncGenerator[Any]:
    if not DSN:
        pytest.skip("TEST_DATABASE_DSN not set")
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        await conn.execute(ASYNC_TASKS_TABLE_SQL)
        await conn.execute("TRUNCATE async_tasks")
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE async_tasks")
        await pool.close()


async def _insert(pool: Any, task_type: str, project: str, deployment: str | None) -> str:
    row = await pool.fetchrow(
        """
        INSERT INTO async_tasks (task_type, project_name, deployment_name, cluster, status)
        VALUES ($1, $2, $3, $4, 'pending')
        RETURNING id
        """,
        task_type,
        project,
        deployment,
        CLUSTER,
    )
    return str(row["id"])


async def _claim_all(service: AsyncTaskService) -> list[tuple[str, str | None]]:
    """Claim repeatedly until none remain; return (task_type, deployment) claimed.

    Claimed tasks stay in status 'claimed' (as in production, until the task
    finishes), so this mirrors what a single worker loop would be allowed to run
    concurrently.
    """
    claimed: list[tuple[str, str | None]] = []
    while True:
        task = await service.claim_next_task(cluster=CLUSTER)
        if task is None:
            return claimed
        claimed.append((task["task_type"], task["deployment_name"]))


async def test_two_sibling_deletes_are_serialized(pool: Any) -> None:
    """Two deletes of the same project (different deployments) -> only one claimable."""
    service = AsyncTaskService(pool=pool, cluster=CLUSTER)
    await _insert(pool, "delete_deployment", "toets-hn7", "pr-36")
    await _insert(pool, "delete_deployment", "toets-hn7", "pr-37")

    claimed = await _claim_all(service)

    assert len(claimed) == 1, f"only one sibling delete should be claimable, got {claimed}"
    assert claimed[0][0] == "delete_deployment"


async def test_mutating_pair_serialized_across_types(pool: Any) -> None:
    """A delete and an upsert on the same project also serialize (both mutate the
    shared project file), even with different deployments."""
    service = AsyncTaskService(pool=pool, cluster=CLUSTER)
    await _insert(pool, "delete_deployment", "toets-hn7", "pr-36")
    await _insert(pool, "upsert_deployment", "toets-hn7", "pr-40")

    claimed = await _claim_all(service)

    assert len(claimed) == 1, f"mutating tasks on one project must serialize, got {claimed}"


async def test_backup_not_blocked_by_mutating_task(pool: Any) -> None:
    """A backup (non-mutating) on the same project is NOT blocked by a running
    delete, so slow restores/backups never queue behind deploys."""
    service = AsyncTaskService(pool=pool, cluster=CLUSTER)
    await _insert(pool, "delete_deployment", "toets-hn7", "pr-36")
    await _insert(pool, "backup", "toets-hn7", "pr-31")

    claimed = await _claim_all(service)

    types = sorted(t for t, _ in claimed)
    assert types == ["backup", "delete_deployment"], f"backup must remain claimable, got {claimed}"


async def test_different_projects_run_in_parallel(pool: Any) -> None:
    """Mutating tasks on different projects are not serialized against each other."""
    service = AsyncTaskService(pool=pool, cluster=CLUSTER)
    await _insert(pool, "delete_deployment", "toets-hn7", "pr-36")
    await _insert(pool, "delete_deployment", "regel-k4c", "pr-99")

    claimed = await _claim_all(service)

    assert len(claimed) == 2, f"cross-project deletes must both be claimable, got {claimed}"


async def test_same_deployment_still_serialized(pool: Any) -> None:
    """The original guard still holds: two tasks on the exact same deployment
    (even non-mutating) do not both run."""
    service = AsyncTaskService(pool=pool, cluster=CLUSTER)
    await _insert(pool, "backup", "toets-hn7", "pr-31")
    await _insert(pool, "restore", "toets-hn7", "pr-31")

    claimed = await _claim_all(service)

    assert len(claimed) == 1, f"same-deployment tasks must serialize, got {claimed}"
