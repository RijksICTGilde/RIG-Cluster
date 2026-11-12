"""Nightly fleet-wide resource auto-tuner.

Once a night (off-peak, when deployments are idle so resize-triggered pod
restarts don't disrupt traffic) this analyzes every project on this cluster and
applies any resource change that clears the deviation deadband.

Checking is cheap and read-only (VPA `.status` reads, a few Prometheus queries); the cost
(git commit, reprocess, ArgoCD sync, pod rollout) falls only on a project that drifts far
enough. The deadband (percentage plus absolute floor, enforced in the analyzer) is what
prevents night-to-night churn, so there is no cooldown: a right-sized project's
recommendation stays within the deadband and produces no change.

A night with many changes at once (the first sweep after enabling, or a big VPA shift)
paces the changed projects apart, so the pod rollouts spread instead of bouncing the whole
fleet at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from opi.core.actor import scheduler_actor, set_actor
from opi.services.catalog.resource_tuning.config import resource_tuning_config
from opi.services.project_store import get_project_store

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Amsterdam")


def _seconds_until_next_run(hour: int, now: datetime) -> float:
    """Seconds from ``now`` until the next occurrence of ``hour``:00 (same tz).

    If ``hour``:00 has already passed today, returns the wait until tomorrow.
    """
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class ResourceTuningScheduler:
    def __init__(self, cluster: str) -> None:
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Resource tuning scheduler started (cluster=%s, nightly at %02d:00 %s)",
            self._cluster,
            resource_tuning_config().hour,
            _TZ.key,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Resource tuning scheduler stopped")

    async def _run(self) -> None:
        """Sleep until the nightly hour, run a sweep, repeat."""
        set_actor(scheduler_actor("resource-tuning"))
        while self._running:
            try:
                delay = _seconds_until_next_run(resource_tuning_config().hour, datetime.now(_TZ))
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

            try:
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in nightly resource tuning sweep")

    async def _sweep(self) -> None:
        """Tune every project on this cluster; the deadband decides what changes."""
        from opi.services.resource_tuning_service import tune_deployment_resources

        projects = get_project_store().get_all()
        names: list[str] = []
        for project in projects:
            if not project.data:
                continue
            name = project.data.get("name", "")
            if not name:
                continue
            if not any(d.get("cluster") == self._cluster for d in project.data.get("deployments", [])):
                continue
            names.append(name)

        logger.info("Nightly resource tuning sweep starting: %d project(s) on cluster %s", len(names), self._cluster)
        changed = 0
        for name in sorted(names):
            if not self._running:
                break
            try:
                result = await tune_deployment_resources(name)
                if result.changes:
                    changed += 1
                    logger.info("Resource tuning: %s - %d component(s) changed", name, len(result.changes))
                    # Pace rollouts so a convergence night doesn't bounce the whole fleet at once.
                    pace_seconds = resource_tuning_config().pace_seconds
                    if pace_seconds > 0:
                        await asyncio.sleep(pace_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Resource tuning failed for project %s", name)

        logger.info("Nightly resource tuning sweep complete: %d/%d project(s) changed", changed, len(names))
