"""Watches the tenant quota (Capsule GlobalCustomQuota) and warns via ntfy when room runs out.

A stopgap until there is real monitoring: one file, wired in ``server.py``, so it can go
again in one cut. OPI's service account cannot read ``gcq`` (it silently gets an empty
list), so the numbers come from the Capsule metrics in the billing Mimir via Grafana.
Growth is compared against the same metric 24h earlier in Mimir, so there is no state
to keep and a restart loses nothing but the dedup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from opi.connectors.grafana_prometheus import (
    GrafanaConnectionError,
    GrafanaQueryError,
    create_grafana_prometheus_connector,
)
from opi.core.config import settings
from opi.services.log_watcher import LogWatchConfig, send_ntfy

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 1800
WARN_PERCENT = 90.0
HIGH_PERCENT = 95.0
MIN_FREE_CPU = 5.0  # cores
MIN_FREE_MEMORY = 5 * 1024**3  # bytes
GROWTH_POINTS = 10.0  # percentage points of the limit in 24h
GROWTH_RELATIVE = 25.0  # percent of the usage 24h ago
GROWTH_RELATIVE_MIN_PERCENT = 20.0  # below this a relative jump is noise

OK, WARNING, HIGH = 0, 1, 2

_METRIC = "capsule_global_custom_quota_resource_{kind}{{tenant='{tenant}'}}"


@dataclass(frozen=True)
class Quota:
    resource: str
    used: float
    limit: float
    used_24h_ago: float | None


def evaluate(quota: Quota) -> tuple[int, list[str]]:
    """Return the alert level of one quota and the reasons for it."""
    if quota.limit <= 0:
        return OK, []
    percent = quota.used / quota.limit * 100
    free = quota.limit - quota.used
    level, reasons = OK, []

    if percent >= HIGH_PERCENT:
        level = HIGH
        reasons.append(f"{percent:.0f}% in gebruik")
    elif percent >= WARN_PERCENT:
        level = WARNING
        reasons.append(f"{percent:.0f}% in gebruik")

    if quota.resource.startswith("cpu-") and free < MIN_FREE_CPU:
        level = max(level, WARNING)
        reasons.append(f"nog {free:.1f} CPU vrij")
    elif quota.resource.startswith("memory-") and free < MIN_FREE_MEMORY:
        level = max(level, WARNING)
        reasons.append(f"nog {free / 1024**3:.1f} GiB vrij")

    if quota.used_24h_ago is not None:
        growth = quota.used - quota.used_24h_ago
        points = growth / quota.limit * 100
        relative = growth / quota.used_24h_ago * 100 if quota.used_24h_ago > 0 else 0.0
        if points >= GROWTH_POINTS or (relative >= GROWTH_RELATIVE and percent >= GROWTH_RELATIVE_MIN_PERCENT):
            level = max(level, WARNING)
            reasons.append(f"stijging {relative:.0f}% in 24 uur" if quota.used_24h_ago > 0 else "stijging in 24 uur")

    return level, reasons


def _format(quota: Quota) -> str:
    in_gib = "memory" in quota.resource or quota.resource.endswith("storage")
    unit = " GiB" if in_gib else ""

    def amount(value: float) -> str:
        return f"{value / 1024**3:.0f}" if in_gib else f"{value:.1f}"

    limit = f"{quota.limit / 1024**3 if in_gib else quota.limit:.0f}{unit}"
    if quota.used_24h_ago is None:
        return f"{amount(quota.used)} van {limit}"
    return f"van {amount(quota.used_24h_ago)} naar {amount(quota.used)}{unit}, maximaal {limit}"


class CapacityWatcher:
    """Every half hour: evaluate the quotas of ``CAPACITY_WATCH_TENANT`` and notify on a new level."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        # One notice per quota per level, not one per tick; dropping back to OK re-arms it.
        self._alerted: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Capacity watcher started (tenant %s, every %ds)", settings.CAPACITY_WATCH_TENANT, INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Capacity watcher stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in capacity watcher")
            try:
                await asyncio.sleep(INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    async def tick(self) -> None:
        try:
            quotas = await self._fetch()
        except (GrafanaConnectionError, GrafanaQueryError) as e:
            logger.info("Capacity watcher: skipping this round, no metrics: %s", e)
            return
        if not quotas:
            logger.warning("Capacity watcher: no quota metrics for tenant %s", settings.CAPACITY_WATCH_TENANT)
            return

        lines: list[str] = []
        top = OK
        for quota in quotas:
            level, reasons = evaluate(quota)
            previous = self._alerted.get(quota.resource, OK)
            self._alerted[quota.resource] = level
            if level > previous:
                top = max(top, level)
                lines.append(f"{quota.resource}: {_format(quota)}, {', '.join(reasons)}")
        if lines:
            await self._send("\n".join(lines), "high" if top == HIGH else "default")

    async def _fetch(self) -> list[Quota]:
        connector = await create_grafana_prometheus_connector()
        tenant = settings.CAPACITY_WATCH_TENANT
        uid = settings.GRAFANA_BILLING_DATASOURCE_UID
        usage = _METRIC.format(kind="usage", tenant=tenant)
        # max by: a second series per resource (e.g. a controller restart) must not pick one at random.
        # max_over_time: one low reading 24h ago, such as Capsule recounting after a restart, is not growth.
        used, limit, ago = [
            _by_resource(await connector.custom_query(query, datasource_uid=uid))
            for query in (
                f"max by (resource) ({usage})",
                f"max by (resource) ({_METRIC.format(kind='limit', tenant=tenant)})",
                f"max by (resource) (max_over_time({usage}[2h] offset 24h))",
            )
        ]
        return [
            Quota(resource=r, used=used[r], limit=limit[r], used_24h_ago=ago.get(r)) for r in sorted(used) if r in limit
        ]

    async def _send(self, body: str, priority: str) -> None:
        if not settings.LOGWATCHER_NTFY_TOPIC:
            logger.warning("Capacity notice not sent: LOGWATCHER_NTFY_TOPIC missing")
            return
        cfg = LogWatchConfig(
            grafana_url=settings.GRAFANA_URL,
            ntfy_topic=settings.LOGWATCHER_NTFY_TOPIC,
            ntfy_server=settings.LOGWATCHER_NTFY_SERVER,
        )
        title = f"ZAD {settings.CLUSTER_MANAGER}: weinig ruimte in tenant {settings.CAPACITY_WATCH_TENANT}"
        await asyncio.to_thread(_post, cfg, title, body, priority)


def _by_resource(results: list[dict[str, Any]]) -> dict[str, float]:
    return {r["metric"]["resource"]: float(r["value"][1]) for r in results if "resource" in r.get("metric", {})}


def _post(cfg: LogWatchConfig, title: str, body: str, priority: str) -> None:
    with httpx.Client() as client:
        send_ntfy(client, cfg, title, body, priority, "chart_with_upwards_trend")
