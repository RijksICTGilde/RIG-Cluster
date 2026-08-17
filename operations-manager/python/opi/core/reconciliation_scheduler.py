"""Nightly reconciliation run.

``opi.jobs.reconciliation.reconcile`` had exactly one caller: the admin endpoint
``POST /api/v2/admin/reconciliation/trigger``. So nothing was ever unmarked or purged
unless someone remembered to call it by hand, and in practice nobody did: manifests that
``delete_deployment`` deferred (step 7 skips them while an ArgoCD finalizer may still need
the source path) and resources confirmed through the orphan sweep sat marked forever, long
past their grace period. This scheduler is the missing tick.

It runs off-peak and does NOT scan for new orphans - detection stays report-first in
``opi.jobs.service_orphan_sweep`` on purpose (a wrong expected set would schedule live
resources for deletion; see the waggl-9et near-miss). All this does is finish work that a
human already authorized: unmark what reappeared, purge what is marked and expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from opi.core.config import settings
from opi.services.project_store import get_project_store

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Amsterdam")


def _seconds_until_next_run(hour: int, now: datetime) -> float:
    """Seconds from ``now`` until the next occurrence of ``hour``:00 (same tz)."""
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class ReconciliationScheduler:
    """Runs the reconciliation job once a night."""

    def __init__(self, cluster: str):
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the nightly scheduler loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Reconciliation scheduler started (cluster=%s, nightly at %02d:00 %s, dry_run=%s)",
            self._cluster,
            settings.RECONCILIATION_HOUR,
            _TZ.key,
            settings.RECONCILIATION_DRY_RUN,
        )

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Reconciliation scheduler stopped")

    async def _run(self) -> None:
        """Sleep until the nightly hour, run reconciliation, repeat."""
        while self._running:
            try:
                delay = _seconds_until_next_run(settings.RECONCILIATION_HOUR, datetime.now(_TZ))
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in nightly reconciliation run")

    async def run_once(self) -> dict[str, object]:
        """Run one reconciliation pass over every loaded project. Returns the results."""
        from opi.jobs.reconciliation import reconcile

        project_yamls = [project.data for project in get_project_store().get_all() if project.data]
        if not project_yamls:
            # An empty store means the projects repo has not loaded, not that every
            # resource is orphaned. Purging against an empty expected set would unmark
            # nothing and could purge anything a human confirmed earlier for the wrong
            # reason, so skip the run entirely.
            logger.warning("Reconciliation skipped: the project store is empty")
            return {"skipped": "empty project store"}

        results = await reconcile(project_yamls=project_yamls, dry_run=settings.RECONCILIATION_DRY_RUN)
        logger.info(
            "Nightly reconciliation done (projects=%d, dry_run=%s): purged=%d, unmarked=%d, errors=%d",
            len(project_yamls),
            settings.RECONCILIATION_DRY_RUN,
            len(results.get("purged", [])),
            len(results.get("unmarked", [])),
            len(results.get("errors", [])),
        )
        return results
