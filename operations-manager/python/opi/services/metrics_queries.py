"""The PromQL this application asks, and how it reads the answers.

One query per question, independent of the backend that carries it: a query written per
connector is a query that can answer differently on the sandbox than in production.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector
from opi.connectors.metrics_backend import MetricsQueryError, MetricsUnavailableError
from opi.utils.naming import generate_unique_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opi.connectors.metrics_backend import MetricsBackend

logger = logging.getLogger(__name__)

#: Factor and rounding that turn a stored value into the one a graph shows, per series.
_SERIES_UNITS: dict[str, tuple[float, int]] = {
    "cpu": (1000.0, 2),
    "memory": (1 / (1024 * 1024), 1),
    "network_in": (1.0, 2),
    "network_out": (1.0, 2),
    "disk_read": (1.0, 2),
    "disk_write": (1.0, 2),
}

#: The same, for the limit and request lines, which are instant values.
_LIMIT_UNITS: dict[str, tuple[float, int]] = {
    "cpu_limit": (1000.0, 0),
    "memory_limit": (1 / (1024 * 1024), 0),
    "memory_request": (1 / (1024 * 1024), 0),
}

#: A frame can carry a null, and ``float("None")`` raises; today's graphs then keep what
#: was already converted and drop the rest of the fetch.
_FETCH_ERRORS = (MetricsUnavailableError, MetricsQueryError, ValueError)


@dataclass(frozen=True)
class TimeWindow:
    """How far back a graph looks, and how far apart its points are."""

    duration_minutes: int = 60
    step_minutes: int = 5

    @property
    def step(self) -> str:
        return f"{self.step_minutes}m"

    def bounds(self) -> tuple[datetime, datetime]:
        """Start and end of the window, ending now."""
        end = datetime.now(UTC)
        return end - timedelta(minutes=self.duration_minutes), end


async def pod_restarts(backend: MetricsBackend, namespace: str | None = None) -> list[dict[str, Any]]:
    """Restart counts per pod as ``{"metric": {"pod": ...}, "value": [timestamp, count]}``."""
    if namespace:
        query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}) by (pod)'
    else:
        query = "sum(kube_pod_container_status_restarts_total) by (namespace, pod)"

    logger.debug(f"Querying pod restarts: {query}")
    return await backend.query(query)


def namespace_selector(namespaces: Iterable[str]) -> str:
    """A PromQL label matcher for a set of namespaces, in the order given."""
    return f'namespace=~"{"|".join(namespaces)}"'


def namespace_usage_queries(namespaces: Iterable[str]) -> dict[str, str]:
    """One total per key over *namespaces*: what the usage cards on every screen show.

    The dashboard and the project page showed the same numbers from their own copies of
    these queries, so a change landed on one screen and not on the other.
    """
    selector = namespace_selector(namespaces)
    queries = {key: f"sum({expr})" for key, expr in _usage_expressions(selector).items()}
    queries["pod_count"] = f"count(kube_pod_info{{{selector}}})"
    return queries


def usage_per_namespace_queries(namespaces: Iterable[str]) -> dict[str, str]:
    """The same totals split per namespace, so one query covers every project at once."""
    selector = namespace_selector(namespaces)
    return {key: f"sum by (namespace) ({expr})" for key, expr in _usage_expressions(selector).items()}


def network_traffic_queries(namespaces: Iterable[str]) -> dict[str, str]:
    """Network bytes per second in and out over *namespaces*, for a range query."""
    selector = namespace_selector(namespaces)
    return {
        "network_in": f"sum(rate(container_network_receive_bytes_total{{{selector}}}[5m]))",
        "network_out": f"sum(rate(container_network_transmit_bytes_total{{{selector}}}[5m]))",
    }


def _usage_expressions(selector: str) -> dict[str, str]:
    """The expression per total, before an aggregation wraps it."""
    return {
        "cpu_cores": f'rate(container_cpu_usage_seconds_total{{{selector},container!=""}}[5m])',
        "cpu_limit_cores": f'kube_pod_container_resource_limits{{{selector},resource="cpu"}}',
        "memory_bytes": f'container_memory_working_set_bytes{{{selector},container!=""}}',
        "memory_limit_bytes": f'kube_pod_container_resource_limits{{{selector},resource="memory"}}',
        "memory_request_bytes": f'kube_pod_container_resource_requests{{{selector},resource="memory"}}',
        "storage_used_bytes": f"kubelet_volume_stats_used_bytes{{{selector}}}",
        "storage_capacity_bytes": f"kubelet_volume_stats_capacity_bytes{{{selector}}}",
    }


def deployment_pod_prefixes(deployment_name: str, components: list[str]) -> dict[str, str]:
    """Component reference to the prefix its pod names carry."""
    return {name: generate_unique_name(deployment_name, name) for name in components}


def workload_pod_prefixes(workloads: list[dict[str, Any]]) -> dict[str, str]:
    """Workload name to pod-name prefix, skipping entries without a name."""
    return {name: name for workload in workloads if (name := workload.get("name"))}


async def component_timeseries(
    backend: MetricsBackend, namespace: str, pod_prefix: str, window: TimeWindow
) -> dict[str, Any]:
    """CPU, memory, network and disk over *window* for the pods matching *pod_prefix*.

    CPU in millicores, memory in MB, network and disk in KB/s, limits and requests as a
    single number. Every key is present even when nothing could be measured.
    """
    result = _empty_timeseries()
    if not backend.is_connected:
        return result

    start_time, end_time = window.bounds()
    try:
        series = await backend.query_range_batch(
            _series_queries(namespace, pod_prefix, window.step), start_time, end_time, window.step
        )
        for key, (factor, rounding) in _SERIES_UNITS.items():
            result[key] = _points(series.get(key, []), factor, rounding)
        result["cpu_timestamps"] = [point["timestamp"] for point in result["cpu"]]
        result["memory_timestamps"] = [point["timestamp"] for point in result["memory"]]
        result["network_timestamps"] = [point["timestamp"] for point in result["network_in"]]
        result["disk_timestamps"] = [point["timestamp"] for point in result["disk_read"]]

        limits = await backend.query_batch(_limit_queries(namespace, pod_prefix, _limit_aggregation(backend)))
        for key, (factor, rounding) in _LIMIT_UNITS.items():
            result[key] = _scalar(limits.get(key, []), factor, rounding)
    except _FETCH_ERRORS as error:
        logger.warning("Failed to get time-series metrics for %s/%s: %s", namespace, pod_prefix, error)

    return result


async def timeseries_per_workload(
    backend: MetricsBackend, namespace: str, pod_prefixes: dict[str, str], window: TimeWindow
) -> dict[str, dict[str, Any]]:
    """:func:`component_timeseries` for every name in *pod_prefixes*, fetched concurrently."""
    gathered = await asyncio.gather(
        *(component_timeseries(backend, namespace, prefix, window) for prefix in pod_prefixes.values())
    )
    return dict(zip(pod_prefixes, gathered, strict=True))


async def discover_workloads(backend: MetricsBackend, namespace: str) -> list[dict[str, Any]]:
    """The deployments, statefulsets and daemonsets in *namespace*, read from ``kube_pod_info``.

    For helm deployments, where the component structure is not known up front. Jobs are
    left out: they are short-lived and need no ongoing monitoring. Each entry holds the
    workload name, its pod count, its pod names and its kind.
    """
    if not backend.is_connected:
        logger.warning("No metrics backend available, cannot discover workloads")
        return []

    query = f'kube_pod_info{{namespace="{namespace}"}}'
    logger.debug(f"Discovering workloads with query: {query}")
    try:
        pods = await backend.query(query)
    except (MetricsUnavailableError, MetricsQueryError) as error:
        logger.warning("Failed to discover workloads in namespace %s: %s", namespace, error)
        return []

    workloads: dict[str, dict[str, Any]] = {}
    for item in pods:
        metric = item.get("metric", {})
        pod_name = metric.get("pod", "")
        created_by_kind = metric.get("created_by_kind", "")
        if not pod_name or created_by_kind == "Job" or _looks_like_a_job_pod(pod_name, created_by_kind):
            continue

        name = extract_workload_name_from_pod(pod_name)
        workload = workloads.setdefault(
            name, {"name": name, "pod_count": 0, "pods": [], "workload_type": _workload_type(created_by_kind)}
        )
        workload["pod_count"] += 1
        workload["pods"].append(pod_name)

    return sorted(workloads.values(), key=lambda workload: workload["name"])


def extract_workload_name_from_pod(pod_name: str) -> str:
    """The workload name a pod belongs to, from its name.

    Deployment pods are ``{deployment}-{replicaset-hash}-{pod-hash}``, statefulset pods
    ``{statefulset}-{ordinal}`` and job pods ``{job}-{hash}``.
    """
    parts = pod_name.split("-")
    if len(parts) < 2:
        return pod_name

    if parts[-1].isdigit():
        return "-".join(parts[:-1])

    if len(parts) >= 3 and _is_hash(parts[-1], 5) and _is_hash(parts[-2]):
        return "-".join(parts[:-2])

    if _is_hash(parts[-1]):
        return "-".join(parts[:-1])

    return pod_name


def _workload_type(created_by_kind: str) -> str:
    """The kind as the page names it; a ReplicaSet owner means a Deployment."""
    return {"ReplicaSet": "Deployment"}.get(created_by_kind, created_by_kind or "Unknown")


def _looks_like_a_job_pod(pod_name: str, created_by_kind: str) -> bool:
    """A pod without an owner kind whose name ends in a single hash, so probably a job."""
    if created_by_kind:
        return False
    parts = pod_name.split("-")
    return (
        len(parts) >= 2
        and _is_hash(parts[-1])
        and not (parts[-1].isdigit() or (len(parts) >= 3 and len(parts[-2]) >= 5))
    )


def _is_hash(part: str, exact_length: int | None = None) -> bool:
    """A pod-name part that reads as a generated hash rather than a name."""
    if not part.isalnum():
        return False
    return len(part) == exact_length if exact_length is not None else len(part) >= 5


#: Where a volume goes from fine to a warning, and from a warning to critical.
_WARNING_FRACTION = 0.8
_CRITICAL_FRACTION = 0.9

_BYTES_PER_GB = 1024 * 1024 * 1024


async def pvc_storage(backend: MetricsBackend, namespace: str, window: TimeWindow) -> dict[str, dict[str, Any]]:
    """Used space over *window* per volume claim in *namespace*, in GB, with its capacity.

    No name matching: every claim the namespace has, so this reads the same on every
    cluster. A claim without a capacity reading gets a capacity of zero.
    """
    result: dict[str, dict[str, Any]] = {}
    if not backend.is_connected:
        return result

    start_time, end_time = window.bounds()
    try:
        used = await backend.query_range_batch(
            {"used": f'kubelet_volume_stats_used_bytes{{namespace="{namespace}"}}'},
            start_time,
            end_time,
            window.step,
        )
        capacities = await backend.query_batch(
            {"capacity": f'kubelet_volume_stats_capacity_bytes{{namespace="{namespace}"}}'}
        )
        capacity_bytes = {
            name: float(series["value"][1])
            for series in capacities.get("capacity", [])
            if (name := series.get("metric", {}).get("persistentvolumeclaim", ""))
        }
        for series in used.get("used", []):
            name = series.get("metric", {}).get("persistentvolumeclaim", "")
            if name and "values" in series:
                result[name] = _volume(series["values"], capacity_bytes.get(name, 0))
    except _FETCH_ERRORS as error:
        logger.warning("Failed to get PVC storage metrics for namespace %s: %s", namespace, error)

    return result


def _volume(values: list[list[Any]], capacity_bytes: float) -> dict[str, Any]:
    capacity_gb = capacity_bytes / _BYTES_PER_GB
    points = [{"timestamp": timestamp, "value": round(float(value) / _BYTES_PER_GB, 3)} for timestamp, value in values]
    return {
        "values": points,
        "timestamps": [point["timestamp"] for point in points],
        "capacity_gb": round(capacity_gb, 2),
        "warning_threshold_gb": round(capacity_gb * _WARNING_FRACTION, 2),
        "critical_threshold_gb": round(capacity_gb * _CRITICAL_FRACTION, 2),
    }


def _series_queries(namespace: str, pod_prefix: str, step: str) -> dict[str, str]:
    pods = f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
    containers = f'{pods},container!=""'
    return {
        "cpu": f"sum(rate(container_cpu_usage_seconds_total{{{containers}}}[{step}]))",
        "memory": f"sum(container_memory_working_set_bytes{{{containers}}})",
        "network_in": f"sum(rate(container_network_receive_bytes_total{{{pods}}}[{step}])) / 1024",
        "network_out": f"sum(rate(container_network_transmit_bytes_total{{{pods}}}[{step}])) / 1024",
        "disk_read": f"sum(rate(container_fs_reads_bytes_total{{{pods}}}[{step}])) / 1024",
        "disk_write": f"sum(rate(container_fs_writes_bytes_total{{{pods}}}[{step}])) / 1024",
    }


def _limit_queries(namespace: str, pod_prefix: str, aggregation: str) -> dict[str, str]:
    pods = f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
    return {
        "cpu_limit": f'{aggregation}(kube_pod_container_resource_limits{{{pods},resource="cpu"}})',
        "memory_limit": f'{aggregation}(kube_pod_container_resource_limits{{{pods},resource="memory"}})',
        "memory_request": f'{aggregation}(kube_pod_container_resource_requests{{{pods},resource="memory"}})',
    }


def _limit_aggregation(backend: MetricsBackend) -> str:
    """``max`` over the pods through Prometheus, ``sum`` through Grafana.

    A difference the two backends have today: with more than one pod the Grafana graph
    draws the limit line N times too high. Kept until the card that makes both ``max``.
    """
    return "sum" if isinstance(backend, GrafanaPrometheusConnector) else "max"


def _points(series: list[dict[str, Any]], factor: float, rounding: int) -> list[dict[str, Any]]:
    if not series or "values" not in series[0]:
        return []
    return [
        {"timestamp": timestamp, "value": round(float(value) * factor, rounding)}
        for timestamp, value in series[0]["values"]
    ]


def _scalar(series: list[dict[str, Any]], factor: float, rounding: int) -> float | None:
    if not series:
        return None
    return round(float(series[0]["value"][1]) * factor, rounding)


def _empty_timeseries() -> dict[str, Any]:
    return {
        "cpu": [],
        "memory": [],
        "network_in": [],
        "network_out": [],
        "disk_read": [],
        "disk_write": [],
        "cpu_limit": None,
        "memory_limit": None,
        "memory_request": None,
        "cpu_timestamps": [],
        "memory_timestamps": [],
        "network_timestamps": [],
        "disk_timestamps": [],
    }
