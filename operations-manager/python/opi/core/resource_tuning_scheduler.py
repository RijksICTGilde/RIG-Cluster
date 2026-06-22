"""Scheduled fleet-wide resource auto-tuner.

Periodically tunes memory and CPU for every project this OPI instance manages,
sourcing recommendations from the VPA recommender where available (else the
Prometheus window). This replaces the unused on-demand tune endpoint as the
primary driver so the whole estate benefits without anyone pressing a button.

Anti-storm design:
  - Per-project cooldown: a project is skipped while its most recent auto-tune
    is younger than RESOURCE_TUNING_COOLDOWN_DAYS (read from resource history,
    so no extra state store).
  - Per-tick cap: at most RESOURCE_TUNING_MAX_PROJECTS_PER_TICK projects are
    tuned per tick, oldest-tuned (and never-tuned) first.
  - The asymmetric deviation gate inside the tuner keeps individual changes
    meaningful (prompt on increases, conservative on decreases).

Urgent memory increases (OOM) are handled out-of-band by the reactive OOM
watcher and are not subject to this cooldown.

Ticks are anchored to wall-clock boundaries of RESOURCE_TUNING_SCHEDULER_INTERVAL
seconds so the firing phase is independent of when OPI started.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from opi.core.config import settings

logger = logging.getLogger(__name__)


def _seconds_to_next_tick(period_seconds: int) -> float:
    """Return seconds until the next wall-clock boundary of `period_seconds`."""
    if period_seconds <= 0:
        return 0.0
    now = time.time()
    remainder = now % period_seconds
    return period_seconds - remainder if remainder else 0.0


def _rotate_batch(items: list[Any], cursor: int, cap: int) -> tuple[list[Any], int]:
    """Take up to ``cap`` items starting at ``cursor`` (wrapping around).

    Returns ``(batch, next_cursor)``. Rotating the start each call guarantees
    every item is reached within ceil(len/cap) calls, instead of always
    re-taking the head - which matters because never-tuned projects share a
    sort key and would otherwise be re-picked forever.
    """
    n = len(items)
    if n == 0:
        return [], 0
    start = cursor % n
    ordered = items[start:] + items[:start]
    batch = ordered[:cap]
    return batch, (start + len(batch)) % n


def _latest_tune_timestamp(project_data: dict[str, Any]) -> datetime | None:
    """Most recent auto-tune/oom-watcher history timestamp across the project.

    Scans both the base component definitions and the per-deployment component
    overrides. Returns None when the project has never been auto-tuned.
    """
    latest: datetime | None = None

    def scan(components: list[dict[str, Any]] | None) -> None:
        nonlocal latest
        for comp in components or []:
            history = (comp.get("resources") or {}).get("history") or []
            for entry in history:
                if entry.get("source") not in ("auto-tune", "oom-watcher"):
                    continue
                ts = entry.get("timestamp")
                if not ts:
                    continue
                try:
                    parsed = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if latest is None or parsed > latest:
                    latest = parsed

    scan(project_data.get("components", []))
    for deployment in project_data.get("deployments", []):
        scan(deployment.get("components", []))
    return latest


class ResourceTuningScheduler:
    """Periodically auto-tunes resources across all projects on this cluster."""

    def __init__(self, cluster: str):
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None
        # Rotating start offset into the due list, so successive ticks cover
        # different projects instead of re-picking the same head every time.
        self._cursor = 0

    async def start(self) -> None:
        """Start the scheduler loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Resource tuning scheduler started (cluster=%s, interval=%ds, cooldown=%dd, cap=%d/tick)",
            self._cluster,
            settings.RESOURCE_TUNING_SCHEDULER_INTERVAL,
            settings.RESOURCE_TUNING_COOLDOWN_DAYS,
            settings.RESOURCE_TUNING_MAX_PROJECTS_PER_TICK,
        )

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Resource tuning scheduler stopped")

    async def _run(self) -> None:
        """Main scheduler loop, ticking on wall-clock boundaries."""
        while self._running:
            try:
                await asyncio.sleep(_seconds_to_next_tick(settings.RESOURCE_TUNING_SCHEDULER_INTERVAL))
            except asyncio.CancelledError:
                break

            try:
                await self._tune_due_projects()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in resource tuning scheduler tick")

    async def _tune_due_projects(self) -> None:
        """Tune the projects whose cooldown has elapsed, capped per tick."""
        from opi.services.project_service import get_project_service
        from opi.services.resource_tuning_service import tune_deployment_resources

        projects = get_project_service().get_all_projects()
        cooldown = timedelta(days=settings.RESOURCE_TUNING_COOLDOWN_DAYS)
        now = datetime.now(UTC)
        epoch = datetime.fromtimestamp(0, UTC)

        # Collect projects with a deployment on this cluster that are past cooldown.
        due: list[tuple[datetime, str]] = []
        for project in projects.values():
            if not project.data:
                continue
            project_name = project.data.get("name", "")
            if not project_name:
                continue
            if not any(d.get("cluster") == self._cluster for d in project.data.get("deployments", [])):
                continue
            last_tune = _latest_tune_timestamp(project.data)
            if last_tune is not None and now - last_tune < cooldown:
                continue
            # Sort key: never-tuned (epoch) first, then oldest tune first.
            due.append((last_tune or epoch, project_name))

        if not due:
            return

        # Order by cooldown age (oldest/never-tuned first), name for determinism.
        due.sort(key=lambda item: (item[0], item[1]))

        # Rotate through the due list so successive ticks cover different
        # projects. Without this, never-tuned projects all share the epoch sort
        # key, a stable sort re-picks the same head every tick, and they only
        # leave the list once a commit advances their cooldown - so idle/unhealthy
        # head projects would starve the rest of the fleet forever.
        batch, self._cursor = _rotate_batch(due, self._cursor, settings.RESOURCE_TUNING_MAX_PROJECTS_PER_TICK)
        logger.info("Resource tuning: %d project(s) due, tuning %d this tick", len(due), len(batch))

        for _, project_name in batch:
            try:
                result = await tune_deployment_resources(project_name)
                if result.changes:
                    logger.info("Resource tuning: %s — %d component(s) changed", project_name, len(result.changes))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Resource tuning failed for project %s", project_name)
