"""Prometheus metrics for OPI memory and resource tracking.

Exposes process metrics, Python GC stats, and custom gauges for internal
data structures via a custom Collector that updates values at scrape time.

Built-in collectors from prometheus_client (auto-registered) provide:
- process_resident_memory_bytes, process_virtual_memory_bytes
- process_open_fds, process_cpu_seconds_total
- python_gc_objects_collected_total, python_gc_collections_total
- python_info

Custom collectors here add OPI-specific internal state:
- Project cache size, task manager state, WebSocket connections
- Rate limiter entries, database pool stats, background tasks
- Optional tracemalloc breakdown by file
"""

import asyncio
import contextlib
import gc
import logging
import os
import time
import tracemalloc
from contextvars import ContextVar
from typing import TYPE_CHECKING

from prometheus_client import Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from prometheus_client.core import Metric

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
# So a test can run the child-process scan against a fake /proc.
_PROC = "/proc"

# Peak memory tracking state (updated by background sampler every second)
_peak_memory_bytes: int = 0
_peak_memory_timestamp: float = 0.0
_last_reset_timestamp: float = 0.0
_sample_task: asyncio.Task[None] | None = None

logger = logging.getLogger(__name__)


# A rollout task takes tens of seconds while the ArgoCD sync inside it takes a few, so
# the question is always "which phase". These are the phases a rollout is divided into;
# the list is closed so a dashboard can enumerate them.
PHASE_GIT_PREPARE = "git_prepare"
PHASE_GENERATE = "generate"
PHASE_PROVISION = "provision"
PHASE_GIT_PUSH = "git_push"
PHASE_ARGO_PROJECT_GATE = "argo_project_gate"
PHASE_ARGO_UMBRELLA = "argo_umbrella"
PHASE_ARGO_WAIT_SYNCED = "argo_wait_synced"
PHASE_ARGO_WAIT_HEALTHY = "argo_wait_healthy"
PHASE_AFTER_SYNC = "after_sync"

ROLLOUT_PHASES = (
    PHASE_GIT_PREPARE,
    PHASE_GENERATE,
    PHASE_PROVISION,
    PHASE_GIT_PUSH,
    PHASE_ARGO_PROJECT_GATE,
    PHASE_ARGO_UMBRELLA,
    PHASE_ARGO_WAIT_SYNCED,
    PHASE_ARGO_WAIT_HEALTHY,
    PHASE_AFTER_SYNC,
)

#: Buckets up to ten minutes: a phase that waits on ArgoCD has a 240-300s timeout, so
#: the default buckets (ending at 10s) would put every wait in +Inf.
_PHASE_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 300, 600, float("inf"))

rollout_phase_seconds = Histogram(
    "opi_rollout_phase_seconds",
    "Duur van een fase van een rollout-taak",
    ["task_type", "phase"],
    buckets=_PHASE_BUCKETS,
)

umbrella_refresh_lost_total = Counter(
    "opi_argocd_umbrella_refresh_lost_total",
    "Refreshes van user-applications die opgingen in een reconcile van voor onze commit",
)

#: The task type the phases of the current task are labeled with. Set by the task worker;
#: paths that run outside a task (schedulers, CLI) observe under "unknown".
_current_task_type: ContextVar[str] = ContextVar("rollout_task_type", default="unknown")


def set_rollout_task_type(task_type: str) -> None:
    """Label the phases measured from here on with this task type."""
    _current_task_type.set(task_type)


def observe_rollout_phase(phase: str, seconds: float) -> None:
    """Record a phase whose duration the caller measured itself."""
    rollout_phase_seconds.labels(_current_task_type.get(), phase).observe(seconds)


@contextlib.contextmanager
def rollout_phase(phase: str) -> Iterator[None]:
    """Measure a rollout phase and record it, also when the phase raises.

    A phase that fails halfway still spent its time, and leaving it out would make the
    histogram flatter than the rollout it describes.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        observe_rollout_phase(phase, time.monotonic() - started)


#: Producers of extra metric families, filled by the layer that owns the state. Core
#: reads nothing upward: the owner registers its own producer.
_metric_sources: list[Callable[[], Iterable[Metric]]] = []


def register_metric_source(source: Callable[[], Iterable[Metric]]) -> None:
    """Add a producer of metric families to the OPI scrape."""
    _metric_sources.append(source)


class OPICollector(Collector):
    """Custom Prometheus collector for OPI internal state.

    Collects point-in-time values at each scrape request, so metrics
    are always fresh without needing a background update loop.
    """

    def collect(self) -> Iterator[Metric]:
        db_active = GaugeMetricFamily(
            "opi_database_pool_active_connections",
            "Number of active database pool connections",
            labels=["pool"],
        )
        db_size = GaugeMetricFamily(
            "opi_database_pool_size",
            "Total size of database connection pool",
            labels=["pool"],
        )
        try:
            from opi.core.database_pools import _pools

            for name, pool in _pools.items():
                try:
                    stats = pool.get_connection_stats()
                    db_active.add_metric([name], stats.get("tracked_active", 0))
                    db_size.add_metric([name], stats.get("pool_size", 0))
                except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
                    logger.debug("Failed to collect stats for pool %s", name, exc_info=True)
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            logger.debug("Failed to collect database pool metrics", exc_info=True)
        yield db_active
        yield db_size

        gc_objects = GaugeMetricFamily(
            "opi_gc_objects",
            "Number of objects tracked by Python garbage collector",
            labels=["generation"],
        )
        try:
            for gen, count in enumerate(gc.get_count()):
                gc_objects.add_metric([str(gen)], count)
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            logger.debug("Failed to collect GC metrics", exc_info=True)
        yield gc_objects

        cgroup_mem = GaugeMetricFamily(
            "opi_container_memory_bytes",
            "Total container memory from cgroup including subprocesses",
        )
        for cgroup_path in [
            "/sys/fs/cgroup/memory.current",  # cgroups v2
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",  # cgroups v1
        ]:
            try:
                with open(cgroup_path) as f:
                    cgroup_mem.add_metric([], int(f.read().strip()))
                break
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
                logger.debug("Failed to read cgroup memory from %s", cgroup_path, exc_info=True)
                break
        yield cgroup_mem

        child_rss = GaugeMetricFamily(
            "opi_child_process_rss_bytes",
            "RSS memory of active child processes",
            labels=["pid", "comm"],
        )
        child_count = GaugeMetricFamily(
            "opi_child_process_count",
            "Number of active child processes",
        )
        my_pid = os.getpid()
        num_children = 0
        try:
            for entry in os.listdir(_PROC):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"{_PROC}/{entry}/stat") as f:
                        stat = f.read()
                    # Parse: pid (comm) state ppid ...
                    # comm can contain spaces/parens, find closing paren
                    close_paren = stat.rfind(")")
                    fields_after_comm = stat[close_paren + 2 :].split()
                    ppid = int(fields_after_comm[1])
                    if ppid != my_pid:
                        continue
                    rss_pages = int(fields_after_comm[21])
                    rss_bytes = rss_pages * _PAGE_SIZE
                    # The process name only, never the command line: that can hold secrets,
                    # and a label value ends up in Prometheus and Grafana (BIO2 8.15).
                    comm = stat[stat.find("(") + 1 : close_paren]
                    child_rss.add_metric([entry, comm], rss_bytes)
                    num_children += 1
                except FileNotFoundError, IndexError, ValueError, PermissionError:
                    continue
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            logger.debug("Failed to scan child processes", exc_info=True)
        child_count.add_metric([], num_children)
        yield child_rss
        yield child_count

        peak_mem = GaugeMetricFamily(
            "opi_peak_memory_bytes",
            "Peak container memory since last reset (sampled every second)",
        )
        peak_mem.add_metric([], _peak_memory_bytes)
        yield peak_mem

        peak_ts = GaugeMetricFamily(
            "opi_peak_memory_timestamp",
            "Unix timestamp when peak memory was recorded",
        )
        peak_ts.add_metric([], _peak_memory_timestamp)
        yield peak_ts

        # A counter plus a snapshot, not a maximum-since-the-previous-scrape: /metrics is
        # open, so a value that disappears on reading would be consumed by any other reader
        # before Prometheus got it. increase() over the counter gives the peak back.
        lag_total = CounterMetricFamily(
            "opi_event_loop_lag_seconds_total",
            "Cumulative asyncio event loop lag in seconds",
        )
        lag_now = GaugeMetricFamily(
            "opi_event_loop_lag_current_seconds",
            "Asyncio event loop lag at scrape time, for a stall still in progress",
        )
        stalls = CounterMetricFamily(
            "opi_event_loop_stalls_total",
            "Number of event loop stalls above EVENT_LOOP_LAG_WARNING_SECONDS",
        )
        try:
            from opi.core import loop_watchdog

            lag_total.add_metric([], loop_watchdog.total_lag())
            lag_now.add_metric([], loop_watchdog.current_lag())
            stalls.add_metric([], loop_watchdog.stall_count())
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            logger.debug("Failed to collect event loop lag", exc_info=True)
        yield lag_total
        yield lag_now
        yield stalls

        connector_delta = GaugeMetricFamily(
            "opi_connector_max_memory_delta_bytes",
            "Maximum memory delta observed during subprocess calls per connector",
            labels=["connector"],
        )
        for name, delta in _connector_max_memory_delta.items():
            connector_delta.add_metric([name], delta)
        yield connector_delta

        connector_calls = GaugeMetricFamily(
            "opi_connector_subprocess_calls_total",
            "Total subprocess calls per connector since last restart",
            labels=["connector"],
        )
        for name, count in _connector_subprocess_calls.items():
            connector_calls.add_metric([name], count)
        yield connector_calls

        for source in _metric_sources:
            try:
                families = list(source())
            except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
                logger.debug("Failed to collect from %s", getattr(source, "__name__", source), exc_info=True)
                continue
            yield from families

    def describe(self) -> list[Metric]:
        """Return empty description; metrics are generated dynamically."""
        return []


class TracmallocCollector(Collector):
    """Optional collector that reports tracemalloc top allocations by file.

    Only active when tracemalloc is started (ENABLE_TRACEMALLOC=true).
    Has ~10-30% memory overhead, so off by default.
    """

    def collect(self) -> Iterator[Metric]:
        if not tracemalloc.is_tracing():
            return

        snapshot = tracemalloc.take_snapshot()
        # Filter out importlib and tracemalloc internals
        snapshot = snapshot.filter_traces(
            [
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
                tracemalloc.Filter(False, tracemalloc.__file__),
            ]
        )
        stats = snapshot.statistics("filename")

        gauge = GaugeMetricFamily(
            "opi_tracemalloc_alloc_bytes",
            "Python memory allocation tracked by tracemalloc, grouped by file",
            labels=["file"],
        )

        for stat in stats[:25]:
            filename = stat.traceback[0].filename if stat.traceback else "unknown"
            # Shorten paths for readability
            for prefix in ["/app/", "/usr/lib/python3.13/", "/usr/local/lib/python3.13/"]:
                if filename.startswith(prefix):
                    filename = filename[len(prefix) :]
                    break
            gauge.add_metric([filename], stat.size)

        yield gauge

    def describe(self) -> list[Metric]:
        """Return empty description; metrics are generated dynamically."""
        return []


def setup_metrics() -> None:
    """Register OPI custom collectors with the default Prometheus registry."""
    from prometheus_client import REGISTRY

    REGISTRY.register(OPICollector())
    logger.info("Registered OPI Prometheus metrics collector")


def setup_tracemalloc() -> None:
    """Start tracemalloc and register the allocation collector.

    Call this during app startup when ENABLE_TRACEMALLOC is true.
    Warning: tracemalloc adds ~10-30% memory overhead.
    """
    from prometheus_client import REGISTRY

    tracemalloc.start()
    REGISTRY.register(TracmallocCollector())
    logger.info("Tracemalloc started and collector registered (adds memory overhead)")


def _read_cgroup_memory() -> int | None:
    """Read current cgroup memory usage. Returns bytes or None if unavailable."""
    for path in [
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ]:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            return None
    return None


async def _peak_memory_sampler() -> None:
    """Background task that samples cgroup memory every second and tracks the peak."""
    global _peak_memory_bytes, _peak_memory_timestamp
    while True:
        try:
            mem = _read_cgroup_memory()
            if mem is not None and mem > _peak_memory_bytes:
                _peak_memory_bytes = mem
                _peak_memory_timestamp = time.time()
        except Exception:  # noqa: BLE001  # a scrape must never fail on one metric
            logger.debug("Failed to sample cgroup memory", exc_info=True)
        await asyncio.sleep(1)


def start_peak_memory_tracking() -> None:
    """Start the background peak memory sampler. Safe to call multiple times."""
    global _sample_task, _peak_memory_bytes, _peak_memory_timestamp, _last_reset_timestamp
    if _sample_task and not _sample_task.done():
        return
    _peak_memory_bytes = 0
    _peak_memory_timestamp = 0.0
    _last_reset_timestamp = time.time()
    _sample_task = asyncio.create_task(_peak_memory_sampler())
    logger.info("Started peak memory sampler (1s interval)")


def stop_peak_memory_tracking() -> None:
    """Stop the background peak memory sampler."""
    global _sample_task
    if _sample_task and not _sample_task.done():
        _sample_task.cancel()
        logger.info("Stopped peak memory sampler")
    _sample_task = None


# Records the max memory delta observed per connector during subprocess calls.
# Key: connector name (e.g. "git", "kubectl", "sops"), Value: max delta in bytes.
_connector_max_memory_delta: dict[str, int] = {}
# Tracks count of subprocess calls per connector
_connector_subprocess_calls: dict[str, int] = {}


class _SubprocessMemoryTracker:
    """Context manager that measures cgroup memory delta around a subprocess call.

    Usage:
        async with track_subprocess_memory("git"):
            stdout, stderr = await process.communicate()
    """

    def __init__(self, connector: str) -> None:
        self.connector = connector
        self.before: int = 0

    async def __aenter__(self) -> _SubprocessMemoryTracker:
        self.before = _read_cgroup_memory() or 0
        return self

    async def __aexit__(self, *exc: object) -> None:
        after = _read_cgroup_memory() or 0
        delta = after - self.before
        _connector_subprocess_calls[self.connector] = _connector_subprocess_calls.get(self.connector, 0) + 1
        if delta > _connector_max_memory_delta.get(self.connector, 0):
            _connector_max_memory_delta[self.connector] = delta
            logger.info(
                f"New peak memory delta for {self.connector}: "
                f"{delta / 1024 / 1024:.1f} MB (before={self.before / 1024 / 1024:.1f} MB, "
                f"after={after / 1024 / 1024:.1f} MB)"
            )


def track_subprocess_memory(connector: str) -> _SubprocessMemoryTracker:
    """Track memory delta around a subprocess call for a given connector name."""
    return _SubprocessMemoryTracker(connector)
