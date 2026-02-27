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
import gc
import logging
import os
import time
import tracemalloc

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Peak memory tracking state (updated by background sampler every second)
_peak_memory_bytes: int = 0
_peak_memory_timestamp: float = 0.0
_last_reset_timestamp: float = 0.0
_sample_task: asyncio.Task[None] | None = None

logger = logging.getLogger(__name__)


class OPICollector(Collector):
    """Custom Prometheus collector for OPI internal state.

    Collects point-in-time values at each scrape request, so metrics
    are always fresh without needing a background update loop.
    """

    def collect(self):
        # --- Project cache ---
        gauge = GaugeMetricFamily(
            "opi_projects_cached",
            "Number of projects in ProjectService in-memory cache",
        )
        try:
            from opi.services.project_service import ProjectService

            if ProjectService._instance is not None:
                gauge.add_metric([], len(ProjectService._instance._projects))
        except Exception:
            logger.debug("Failed to collect project cache metrics", exc_info=True)
        yield gauge

        # --- Task manager state ---
        task_projects = GaugeMetricFamily(
            "opi_task_projects_tracked",
            "Number of projects tracked in task manager",
        )
        task_managers = GaugeMetricFamily(
            "opi_task_managers_active",
            "Number of active TaskProgressManager instances",
        )
        try:
            from opi.core.task_manager import _project_managers, _projects

            task_projects.add_metric([], len(_projects))
            task_managers.add_metric([], len(_project_managers))
        except Exception:
            logger.debug("Failed to collect task manager metrics", exc_info=True)
        yield task_projects
        yield task_managers

        # --- WebSocket connections ---
        ws_global = GaugeMetricFamily(
            "opi_websocket_connections_global",
            "Number of active global WebSocket connections",
        )
        ws_users = GaugeMetricFamily(
            "opi_websocket_connections_users",
            "Number of distinct users with active WebSocket connections",
        )
        try:
            from opi.api.logs_websocket_router import (
                _active_connections,
                _global_connections,
            )

            ws_global.add_metric([], len(_global_connections))
            ws_users.add_metric([], len(_active_connections))
        except Exception:
            logger.debug("Failed to collect WebSocket metrics", exc_info=True)
        yield ws_global
        yield ws_users

        # --- Rate limiter ---
        rl_clients = GaugeMetricFamily(
            "opi_rate_limiter_tracked_clients",
            "Number of clients tracked by the rate limiter",
        )
        try:
            from opi.api.router import subdomain_check_rate_limiter

            rl_clients.add_metric([], len(subdomain_check_rate_limiter._tokens))
        except Exception:
            logger.debug("Failed to collect rate limiter metrics", exc_info=True)
        yield rl_clients

        # --- Database pools ---
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
                except Exception:
                    logger.debug("Failed to collect stats for pool %s", name, exc_info=True)
        except Exception:
            logger.debug("Failed to collect database pool metrics", exc_info=True)
        yield db_active
        yield db_size

        # --- Background monitoring tasks ---
        bg_tasks = GaugeMetricFamily(
            "opi_background_tasks_active",
            "Number of active asyncio background monitoring tasks",
        )
        try:
            from opi.core.task_manager import (
                _active_app_monitoring_tasks,
                _active_monitoring_tasks,
            )

            active_count = sum(1 for t in _active_monitoring_tasks.values() if not t.done()) + sum(
                1 for t in _active_app_monitoring_tasks.values() if not t.done()
            )
            bg_tasks.add_metric([], active_count)
        except Exception:
            logger.debug("Failed to collect background task metrics", exc_info=True)
        yield bg_tasks

        # --- GC stats ---
        gc_objects = GaugeMetricFamily(
            "opi_gc_objects",
            "Number of objects tracked by Python garbage collector",
            labels=["generation"],
        )
        try:
            for gen, count in enumerate(gc.get_count()):
                gc_objects.add_metric([str(gen)], count)
        except Exception:
            logger.debug("Failed to collect GC metrics", exc_info=True)
        yield gc_objects

        # --- Container cgroup memory (what Kubernetes sees) ---
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
            except Exception:
                logger.debug("Failed to read cgroup memory from %s", cgroup_path, exc_info=True)
                break
        yield cgroup_mem

        # --- Child process memory (subprocess RSS at scrape time) ---
        child_rss = GaugeMetricFamily(
            "opi_child_process_rss_bytes",
            "RSS memory of active child processes",
            labels=["pid", "command"],
        )
        child_count = GaugeMetricFamily(
            "opi_child_process_count",
            "Number of active child processes",
        )
        my_pid = os.getpid()
        num_children = 0
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat") as f:
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
                    with open(f"/proc/{entry}/cmdline") as f:
                        cmdline = f.read().replace("\0", " ").strip()[:100]
                    child_rss.add_metric([entry, cmdline], rss_bytes)
                    num_children += 1
                except (FileNotFoundError, IndexError, ValueError, PermissionError):
                    continue
        except Exception:
            logger.debug("Failed to scan child processes", exc_info=True)
        child_count.add_metric([], num_children)
        yield child_rss
        yield child_count

        # --- Peak memory (sampled every second by background task) ---
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

        # --- Per-connector subprocess memory deltas ---
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

    def describe(self):
        """Return empty description; metrics are generated dynamically."""
        return []


class TracmallocCollector(Collector):
    """Optional collector that reports tracemalloc top allocations by file.

    Only active when tracemalloc is started (ENABLE_TRACEMALLOC=true).
    Has ~10-30% memory overhead, so off by default.
    """

    def collect(self):
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

    def describe(self):
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
        except Exception:
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
        except Exception:
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


# --- Per-connector subprocess memory tracking ---
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

    async def __aenter__(self) -> "_SubprocessMemoryTracker":
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
