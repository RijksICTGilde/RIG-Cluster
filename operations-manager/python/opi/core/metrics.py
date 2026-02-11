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

import gc
import logging
import tracemalloc

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

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

        # --- Background tasks ---
        bg_tasks = GaugeMetricFamily(
            "opi_background_tasks_active",
            "Number of active asyncio background tasks",
        )
        try:
            from opi.core.simple_background import (
                _background_tasks as sb_tasks,
            )
            from opi.core.task_manager import (
                _background_tasks as tm_tasks,
            )

            bg_tasks.add_metric([], len(tm_tasks) + len(sb_tasks))
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
