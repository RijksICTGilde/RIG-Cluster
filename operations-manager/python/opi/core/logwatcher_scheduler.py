"""Scheduler for the in-app OPI log watcher.

Every ``LOGWATCHER_INTERVAL_SECONDS`` this runs one triage cycle over the OPI
production logs (Loki) and pushes an ntfy notification when something unexpected
and not-yet-alerted shows up. It runs the exact same pipeline as the standalone
CLI (``scripts/log_watch/watch.py``) via ``opi.services.log_watcher.run_cycle`` -
just with no Claude triage and in-memory dedup state.

The pipeline is synchronous (httpx), so the sweep is offloaded to a thread with
``asyncio.to_thread`` to keep the event loop free. Dedup state is a plain dict on
the instance: it resets on an OPI restart, which at worst repeats one alert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from opi.core.config import settings
from opi.services.log_watcher import LogWatchConfig, run_cycle

logger = logging.getLogger(__name__)


class LogwatcherScheduler:
    """Periodically triages OPI production logs and notifies via ntfy."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._state: dict[str, str] = {}  # signature -> ISO timestamp (in-memory dedup)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Log watcher scheduler started (every %ds, ntfy %s/%s)",
            settings.LOGWATCHER_INTERVAL_SECONDS,
            settings.LOGWATCHER_NTFY_SERVER,
            settings.LOGWATCHER_NTFY_TOPIC,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Log watcher scheduler stopped")

    def _build_config(self) -> LogWatchConfig:
        return LogWatchConfig(
            grafana_url=settings.GRAFANA_URL,
            ntfy_topic=settings.LOGWATCHER_NTFY_TOPIC or "",
            ntfy_server=settings.LOGWATCHER_NTFY_SERVER,
            namespace=settings.LOGWATCHER_NAMESPACE,
            container=settings.LOGWATCHER_CONTAINER,
            window=settings.LOGWATCHER_WINDOW,
            dedup_hours=settings.LOGWATCHER_DEDUP_HOURS,
            # Leave datasource_uid unset: GRAFANA_DATASOURCE_UID points at Mimir
            # (metrics), not Loki, so run_cycle auto-discovers the Loki datasource.
        )

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.LOGWATCHER_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            try:
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in log watcher sweep")

    async def _sweep(self) -> None:
        if not settings.GRAFANA_TOKEN:
            logger.warning("Log watcher sweep skipped: GRAFANA_TOKEN not configured")
            return
        if not settings.LOGWATCHER_NTFY_TOPIC:
            logger.warning("Log watcher sweep skipped: LOGWATCHER_NTFY_TOPIC not configured")
            return
        # Deterministic grouped body (no Claude in the pod); sync pipeline off-thread.
        await asyncio.to_thread(run_cycle, self._build_config(), settings.GRAFANA_TOKEN, self._state, triage_fn=None)
