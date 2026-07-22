"""Tests for the project-store fallback reconcile poll.

The poll is the bounded window for out-of-band revocation: a member removed or
an invite key revoked by pushing straight to zad-projects must reach this
instance's cache without an explicit refresh or restart. So the loop must (a)
actually call reconcile every tick and (b) survive a transient reconcile error
-- a loop that dies on the first network blip silently reopens the unbounded
revocation gap.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services import project_store
from opi.services.project_store import _reconcile_poll_loop, start_reconcile_poll, stop_reconcile_poll


@pytest.fixture(autouse=True)
def _clean_poll_task():
    yield
    stop_reconcile_poll()


@pytest.mark.asyncio
async def test_poll_loop_calls_reconcile_and_survives_errors() -> None:
    store = MagicMock()
    store.reconcile = AsyncMock(side_effect=[RuntimeError("transient git error"), None])

    ticks = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 2:
            raise asyncio.CancelledError

    with (
        patch.object(project_store, "get_project_store", return_value=store),
        patch.object(project_store.asyncio, "sleep", fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await _reconcile_poll_loop(300)

    # Two ticks ran: the first reconcile raised and did NOT end the loop,
    # the second ran normally.
    assert store.reconcile.await_count == 2


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels() -> None:
    with patch.object(project_store.settings, "PROJECT_STORE_RECONCILE_INTERVAL_SECONDS", 300):
        start_reconcile_poll()
        task = project_store._reconcile_poll_task
        assert task is not None
        assert not task.done()

        # Second start reuses the running task instead of spawning another.
        start_reconcile_poll()
        assert project_store._reconcile_poll_task is task

        stop_reconcile_poll()
        assert project_store._reconcile_poll_task is None
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_interval_zero_disables_the_poll() -> None:
    with patch.object(project_store.settings, "PROJECT_STORE_RECONCILE_INTERVAL_SECONDS", 0):
        start_reconcile_poll()
        assert project_store._reconcile_poll_task is None
