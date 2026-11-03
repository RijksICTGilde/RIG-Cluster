"""Measured size of the volumes and databases on the project map.

Read from the metrics store with the same ``last_over_time(...[6h])`` window as the
shared-services page (``opi/services/gedeelde_diensten.py``): a volume or database that was
not scraped in the last minutes still has its last known size. A failed or slow query
yields no measurement; the map is drawn without it rather than not at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from opi.connectors.grafana_prometheus import GrafanaConnectionError, GrafanaQueryError
from opi.connectors.prometheus import PrometheusConnectionError, PrometheusQueryError, get_metrics_connector

logger = logging.getLogger(__name__)

#: The map waits this long for the metrics store; a slow store costs the sizes, not the map.
TIMEOUT_SECONDS = 5.0

#: Names go into a PromQL regex unescaped, so only names that need no escaping qualify.
#: Kubernetes namespaces and generated database names always do.
_SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")

_VOLUME_USED = (
    "max by (namespace, persistentvolumeclaim) "
    '(last_over_time(kubelet_volume_stats_used_bytes{{namespace=~"{namespaces}"}}[6h]))'
)
_VOLUME_CAPACITY = (
    "max by (namespace, persistentvolumeclaim) "
    '(last_over_time(kubelet_volume_stats_capacity_bytes{{namespace=~"{namespaces}"}}[6h]))'
)
_DATABASE_SIZE = 'max by (datname) (last_over_time(cnpg_pg_database_size_bytes{{datname=~"{databases}"}}[6h]))'

_QUERY_ERRORS = (
    PrometheusQueryError,
    PrometheusConnectionError,
    GrafanaQueryError,
    GrafanaConnectionError,
    TimeoutError,
)


@dataclass
class MeasuredUsage:
    """Sizes in bytes. A missing key means "not measured", never zero."""

    volume_used: dict[tuple[str, str], float] = field(default_factory=dict)
    volume_capacity: dict[tuple[str, str], float] = field(default_factory=dict)
    database_size: dict[str, float] = field(default_factory=dict)


def _value(series: dict[str, Any]) -> float | None:
    raw = series.get("value")
    if not raw or len(raw) < 2:
        return None
    try:
        number = float(raw[1])
    except TypeError, ValueError:
        return None
    return None if number != number else number  # NaN is no measurement


def _by_labels(results: list[dict[str, Any]], *labels: str) -> dict[tuple[str, ...], float]:
    table: dict[tuple[str, ...], float] = {}
    for series in results:
        metric = series.get("metric", {})
        value = _value(series)
        if value is not None:
            table[tuple(str(metric.get(label, "")) for label in labels)] = value
    return table


async def measure_usage(namespaces: set[str], databases: set[str]) -> MeasuredUsage:
    """The sizes of every volume in *namespaces* and of the named *databases*."""
    namespaces = {name for name in namespaces if _SAFE_NAME.match(name)}
    databases = {name for name in databases if _SAFE_NAME.match(name)}
    usage = MeasuredUsage()
    if not namespaces and not databases:
        return usage

    queries: dict[str, str] = {}
    if namespaces:
        joined = "|".join(sorted(namespaces))
        queries["used"] = _VOLUME_USED.format(namespaces=joined)
        queries["capacity"] = _VOLUME_CAPACITY.format(namespaces=joined)
    if databases:
        queries["database"] = _DATABASE_SIZE.format(databases="|".join(sorted(databases)))

    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            connector = await get_metrics_connector()
            names = list(queries)
            results = await asyncio.gather(*(connector.custom_query(queries[name]) for name in names))
    except _QUERY_ERRORS as error:
        logger.warning("map: sizes not measured: %s", error)
        return usage

    measured = dict(zip(names, results, strict=True))
    for (namespace, pvc), value in _by_labels(measured.get("used", []), "namespace", "persistentvolumeclaim").items():
        usage.volume_used[(namespace, pvc)] = value
    for (namespace, pvc), value in _by_labels(
        measured.get("capacity", []), "namespace", "persistentvolumeclaim"
    ).items():
        usage.volume_capacity[(namespace, pvc)] = value
    for (database,), value in _by_labels(measured.get("database", []), "datname").items():
        usage.database_size[database] = value
    return usage


def format_bytes(value: float) -> str:
    """A size as a reader says it: ``340 MB``, ``1,2 GB``. Decimal units, Dutch comma."""
    for unit, factor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if value >= factor:
            amount = value / factor
            text = f"{amount:.1f}" if amount < 10 else f"{amount:.0f}"
            return f"{text.replace('.', ',')} {unit}"
    return f"{value:.0f} B"
