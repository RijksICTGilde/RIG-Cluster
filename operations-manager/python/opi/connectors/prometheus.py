"""
Prometheus connector for querying metrics from Prometheus.

This module provides functionality to interact with Prometheus for retrieving
cluster and application metrics.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from typing import Any

# Imported from the submodule, not the package root: the package root exposes its
# classes through a module-level __getattr__ shim, so a type checker resolves
# `prometheus_api_client.PrometheusConnect` to the union of everything that shim can
# return (Metric, MetricsList, ...) and then rejects perfectly valid constructor
# arguments. The submodule path resolves to the class itself.
from prometheus_api_client.prometheus_connect import PrometheusConnect

from opi.core.config import settings
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


class PrometheusConnectionError(Exception):
    """Exception raised when Prometheus connection fails."""


class PrometheusQueryError(Exception):
    """Exception raised when a Prometheus query fails."""


class PrometheusConnector:
    """Connector for interacting with Prometheus for metrics retrieval."""

    _instance: PrometheusConnector | None = None
    is_connected: bool = False
    prom: Any  # PrometheusConnect instance (untyped library)
    _initialized: bool
    _prometheus_url: str

    def __new__(cls) -> PrometheusConnector:
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the Prometheus connector.

        Creates the HTTP client but does not test the connection - PrometheusConnect
        is a stateless HTTP client so there is no persistent connection to verify.
        Each query is an independent HTTP request that succeeds or raises on its own.
        """
        if self._initialized:
            return

        self._initialized = True
        self._prometheus_url = getattr(settings, "PROMETHEUS_URL", "http://prometheus.rig-system:9090")
        logger.info(f"Initializing PrometheusConnector with URL: {self._prometheus_url}")

        self.prom = PrometheusConnect(url=self._prometheus_url, disable_ssl=True)
        PrometheusConnector.is_connected = True

    @property
    def prometheus_url(self) -> str:
        """Return the configured Prometheus URL."""
        return self._prometheus_url

    def reconnect(self) -> bool:
        """Kept for backward compatibility with startup code. Always returns True.

        There is no persistent connection - PrometheusConnect is a stateless HTTP
        client. Queries will succeed or fail on their own.
        """
        PrometheusConnector.is_connected = True
        return True

    def _ensure_connected(self) -> None:
        """Kept for backward compatibility. No-op - there is no connection to check."""

    def get_cpu_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """
        Get CPU usage metrics grouped by pod.

        Args:
            namespace: Optional namespace to filter by. If None, returns all namespaces.

        Returns:
            List of metric results with pod CPU usage.
        """
        self._ensure_connected()

        if namespace:
            query = (
                f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",container!=""}}[5m])) by (pod)'
            )
        else:
            query = 'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m])) by (namespace, pod)'

        logger.debug(f"Querying CPU usage: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to query CPU usage: {e}")
            raise PrometheusQueryError(f"Failed to query CPU usage: {e}") from e

    def get_memory_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """
        Get memory usage metrics grouped by pod.

        Args:
            namespace: Optional namespace to filter by. If None, returns all namespaces.

        Returns:
            List of metric results with pod memory usage in bytes.
        """
        self._ensure_connected()

        if namespace:
            query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",container!=""}}) by (pod)'
        else:
            query = 'sum(container_memory_working_set_bytes{container!=""}) by (namespace, pod)'

        logger.debug(f"Querying memory usage: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to query memory usage: {e}")
            raise PrometheusQueryError(f"Failed to query memory usage: {e}") from e

    def get_pod_count(self, namespace: str | None = None) -> int:
        """
        Get the count of running pods.

        Args:
            namespace: Optional namespace to filter by. If None, returns total count.

        Returns:
            Number of running pods.
        """
        self._ensure_connected()

        query = f'count(kube_pod_info{{namespace="{namespace}"}})' if namespace else "count(kube_pod_info)"

        logger.debug(f"Querying pod count: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            if result and len(result) > 0:
                return int(float(result[0]["value"][1]))
            return 0
        except Exception as e:
            logger.error(f"Failed to query pod count: {e}")
            raise PrometheusQueryError(f"Failed to query pod count: {e}") from e

    def get_pod_restarts(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """
        Get pod restart counts.

        Args:
            namespace: Optional namespace to filter by. If None, returns all namespaces.

        Returns:
            List of metric results with pod restart counts.
        """
        self._ensure_connected()

        if namespace:
            query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}) by (pod)'
        else:
            query = "sum(kube_pod_container_status_restarts_total) by (namespace, pod)"

        logger.debug(f"Querying pod restarts: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to query pod restarts: {e}")
            raise PrometheusQueryError(f"Failed to query pod restarts: {e}") from e

    def get_network_receive_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """
        Get network receive bytes rate.

        Args:
            namespace: Optional namespace to filter by. If None, returns all namespaces.

        Returns:
            List of metric results with network receive rates.
        """
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_receive_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_receive_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network receive: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to query network receive: {e}")
            raise PrometheusQueryError(f"Failed to query network receive: {e}") from e

    def get_network_transmit_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """
        Get network transmit bytes rate.

        Args:
            namespace: Optional namespace to filter by. If None, returns all namespaces.

        Returns:
            List of metric results with network transmit rates.
        """
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_transmit_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network transmit: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to query network transmit: {e}")
            raise PrometheusQueryError(f"Failed to query network transmit: {e}") from e

    def get_cluster_overview(self) -> dict[str, Any]:
        """
        Get a high-level overview of cluster metrics.

        Returns:
            Dictionary with cluster overview metrics including:
            - total_cpu_usage: Total CPU usage across all pods
            - total_memory_bytes: Total memory usage across all pods
            - pod_count: Total number of pods
            - namespace_count: Number of namespaces with running pods
        """
        self._ensure_connected()

        overview = {
            "total_cpu_usage": 0.0,
            "total_memory_bytes": 0,
            "pod_count": 0,
            "namespace_count": 0,
        }

        try:
            # Total CPU usage
            cpu_result: list[dict[str, Any]] = self.prom.custom_query(
                'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
            )
            if cpu_result:
                overview["total_cpu_usage"] = float(cpu_result[0]["value"][1])

            # Total memory usage
            memory_result: list[dict[str, Any]] = self.prom.custom_query(
                'sum(container_memory_working_set_bytes{container!=""})'
            )
            if memory_result:
                overview["total_memory_bytes"] = int(float(memory_result[0]["value"][1]))

            # Pod count
            pod_result: list[dict[str, Any]] = self.prom.custom_query("count(kube_pod_info)")
            if pod_result:
                overview["pod_count"] = int(float(pod_result[0]["value"][1]))

            # Namespace count (unique namespaces with pods)
            ns_result: list[dict[str, Any]] = self.prom.custom_query("count(count by (namespace) (kube_pod_info))")
            if ns_result:
                overview["namespace_count"] = int(float(ns_result[0]["value"][1]))

        except Exception as e:
            logger.error(f"Failed to get cluster overview: {e}")
            raise PrometheusQueryError(f"Failed to get cluster overview: {e}") from e

        return overview

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        """
        Execute a custom PromQL query.

        Declared async to match GrafanaPrometheusConnector.custom_query so callers
        can await a single interface regardless of METRICS_BACKEND.

        IN EEN THREAD, EN DAT IS GEEN VERSIERING. prometheus_api_client is synchroon en
        praat via ``requests``; rechtstreeks aangeroepen blokkeert hij de event loop voor
        de duur van het verzoek. Hier stond dat dat mocht "omdat metriekqueries
        laagfrequent zijn" -- maar de frequentie is niet het punt, de DUUR is het. Een
        Prometheus die niet oplost kost per query de volledige DNS- en retryketen, en zolang
        die loopt handelt de applicatie GEEN ENKEL ander verzoek af.

        Gemeten op /admin/diensten: dat scherm haalt drie blokken lui op en belooft dat een
        kapot blok alleen dat blok kost. Het Keycloak-blok praat rechtstreeks met deze
        connector, en met een onbereikbare Prometheus bleven de twee andere blokken op
        "wordt opgehaald..." staan tot de retries op waren -- de belofte van de pagina precies
        omgedraaid. Zeven browsertests stonden daarop rood.
        """
        self._ensure_connected()

        logger.debug(f"Executing custom query: {query}")

        try:
            result: list[dict[str, Any]] = await asyncio.to_thread(self.prom.custom_query, query)
            return result
        except Exception as e:
            logger.error(f"Failed to execute custom query: {e}")
            raise PrometheusQueryError(f"Failed to execute custom query: {e}") from e

    def discover_metric_names(self, match_selector: str) -> list[str]:
        """
        Discover available metric names matching a label selector.

        Uses the Prometheus ``/api/v1/series`` endpoint which searches the TSDB
        index directly.  This is essential for jobs with long scrape intervals
        (e.g. 2 hours) where PromQL instant queries would return empty results
        due to the default 5-minute staleness window.

        Args:
            match_selector: A Prometheus label matcher, e.g. '{job="minio"}'.

        Returns:
            Sorted list of metric names.
        """
        import requests

        url = f"{self._prometheus_url.rstrip('/')}/api/v1/series"
        logger.debug(f"Discovering metric names via series API: match[]={match_selector}")

        try:
            response = requests.get(url, params={"match[]": match_selector}, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise PrometheusQueryError(f"Series API returned status: {data.get('status')}")

            names: set[str] = set()
            for series in data.get("data", []):
                name = series.get("__name__")
                if name:
                    names.add(name)

            return sorted(names)
        except requests.RequestException as e:
            logger.error(f"Failed to discover metric names for {match_selector}: {e}")
            raise PrometheusQueryError(f"Failed to discover metric names: {e}") from e

    def query_range(self, query: str, start_time: str, end_time: str, step: str) -> list[dict[str, Any]]:
        """
        Execute a range query to get time-series data.

        Args:
            query: The PromQL query to execute.
            start_time: Start time (RFC3339 or Unix timestamp).
            end_time: End time (RFC3339 or Unix timestamp).
            step: Query resolution step (e.g., "5m", "1h").

        Returns:
            List of metric results with time-series values.
        """
        self._ensure_connected()

        logger.debug(f"Executing range query: {query} from {start_time} to {end_time} step {step}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query_range(
                query=query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )
            return result
        except Exception as e:
            logger.error(f"Failed to execute range query: {e}")
            raise PrometheusQueryError(f"Failed to execute range query: {e}") from e

    def get_component_metrics(self, namespace: str, pod_prefix: str, time_range: str = "6h") -> dict[str, float | None]:
        """
        Get aggregated metrics for a specific component (identified by pod name prefix).

        Args:
            namespace: The Kubernetes namespace
            pod_prefix: Prefix of pod names to match (e.g., "production-frontend")
            time_range: Time range for rate calculations (default: 6h)

        Returns:
            Dictionary with metrics:
            - cpu_cores: Average CPU usage in cores over the time range
            - memory_bytes: Current memory usage in bytes
            - memory_mb: Current memory usage in MB
            - requests_per_second: HTTP requests per second (if available)
        """
        if not PrometheusConnector.is_connected:
            return {
                "cpu_cores": None,
                "memory_bytes": None,
                "memory_mb": None,
                "requests_per_second": None,
            }

        metrics: dict[str, float | None] = {
            "cpu_cores": None,
            "memory_bytes": None,
            "memory_mb": None,
            "requests_per_second": None,
        }

        try:
            # CPU usage (average over time range, in cores)
            cpu_query = (
                f"avg(rate(container_cpu_usage_seconds_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f"}}[{time_range}]))"
            )
            cpu_result: list[dict[str, Any]] = self.prom.custom_query(cpu_query)
            if cpu_result and len(cpu_result) > 0:
                metrics["cpu_cores"] = float(cpu_result[0]["value"][1])

            # Memory usage (current, in bytes)
            memory_query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""}})'
            memory_result: list[dict[str, Any]] = self.prom.custom_query(memory_query)
            if memory_result and len(memory_result) > 0:
                memory_bytes = float(memory_result[0]["value"][1])
                metrics["memory_bytes"] = memory_bytes
                metrics["memory_mb"] = memory_bytes / (1024 * 1024)

            # HTTP requests per second (if metrics are exposed)
            # Try common metric names used by different frameworks
            for req_metric in [
                "http_requests_total",
                "http_server_requests_seconds_count",
                "promhttp_metric_handler_requests_total",
            ]:
                req_query = f'sum(rate({req_metric}{{namespace="{namespace}",pod=~"{pod_prefix}.*"}}[{time_range}]))'
                try:
                    req_result: list[dict[str, Any]] = self.prom.custom_query(req_query)
                    if req_result and len(req_result) > 0:
                        value = float(req_result[0]["value"][1])
                        if value > 0:
                            metrics["requests_per_second"] = value
                            break
                except Exception:
                    logger.debug("Metric %s not available for %s/%s, trying next", req_metric, namespace, pod_prefix)
                    continue

        except Exception as e:
            logger.warning(f"Failed to get component metrics for {namespace}/{pod_prefix}: {e}")

        return metrics

    def get_deployment_component_metrics(
        self, namespace: str, components: list[str], deployment_name: str, time_range: str = "6h"
    ) -> dict[str, dict[str, float | None]]:
        """
        Get metrics for all components in a deployment.

        Args:
            namespace: The Kubernetes namespace
            components: List of component names (component references)
            deployment_name: Name of the deployment
            time_range: Time range for rate calculations (default: 6h)

        Returns:
            Dictionary mapping component names to their metrics
        """
        pod_prefixes = {name: generate_unique_name(deployment_name, name) for name in components}

        with ThreadPoolExecutor(max_workers=len(pod_prefixes)) as executor:
            futures = {
                name: executor.submit(self.get_component_metrics, namespace, prefix, time_range)
                for name, prefix in pod_prefixes.items()
            }
            return {name: future.result() for name, future in futures.items()}

    def get_component_metrics_timeseries(
        self, namespace: str, pod_prefix: str, duration_minutes: int = 60, step_minutes: int = 5
    ) -> dict[str, Any]:
        """
        Get time-series metrics for a component over a duration.

        Args:
            namespace: The Kubernetes namespace
            pod_prefix: Prefix of pod names to match
            duration_minutes: How far back to query (default: 60 minutes)
            step_minutes: Interval between data points (default: 5 minutes)

        Returns:
            Dictionary with time-series data:
            - cpu: List of {timestamp, value} dicts (CPU in millicores)
            - memory: List of {timestamp, value} dicts (memory in MB)
            - labels: List of time labels for the chart
            - cpu_limit: CPU limit in millicores (or None)
            - memory_limit: Memory limit in MB (or None)
        """
        from datetime import datetime, timedelta

        if not PrometheusConnector.is_connected:
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

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(minutes=duration_minutes)
        step = f"{step_minutes}m"

        result: dict[str, Any] = {
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

        try:
            # CPU usage time-series (in cores, we'll convert to millicores)
            cpu_query = (
                f"sum(rate(container_cpu_usage_seconds_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f"}}[{step}]))"
            )
            logger.debug(f"Prometheus CPU query: {cpu_query}")
            cpu_result = self.prom.custom_query_range(
                query=cpu_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )
            logger.debug(f"Prometheus CPU result: {cpu_result}")

            if cpu_result and len(cpu_result) > 0 and "values" in cpu_result[0]:
                for ts, value in cpu_result[0]["values"]:
                    cpu_millicores = float(value) * 1000  # Convert to millicores
                    result["cpu"].append(
                        {
                            "timestamp": ts,
                            "value": round(cpu_millicores, 2),
                        }
                    )

            # Memory usage time-series (in bytes, we'll convert to MB)
            memory_query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""}})'
            memory_result = self.prom.custom_query_range(
                query=memory_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if memory_result and len(memory_result) > 0 and "values" in memory_result[0]:
                for ts, value in memory_result[0]["values"]:
                    memory_mb = float(value) / (1024 * 1024)  # Convert to MB
                    result["memory"].append(
                        {
                            "timestamp": ts,
                            "value": round(memory_mb, 1),
                        }
                    )

            # Network receive (bytes/sec, convert to KB/s)
            network_in_query = (
                f"sum(rate(container_network_receive_bytes_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f"}}[{step}])) / 1024"
            )
            network_in_result = self.prom.custom_query_range(
                query=network_in_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if network_in_result and len(network_in_result) > 0 and "values" in network_in_result[0]:
                for ts, value in network_in_result[0]["values"]:
                    result["network_in"].append(
                        {
                            "timestamp": ts,
                            "value": round(float(value), 2),
                        }
                    )

            # Network transmit (bytes/sec, convert to KB/s)
            network_out_query = (
                f"sum(rate(container_network_transmit_bytes_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f"}}[{step}])) / 1024"
            )
            network_out_result = self.prom.custom_query_range(
                query=network_out_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if network_out_result and len(network_out_result) > 0 and "values" in network_out_result[0]:
                for ts, value in network_out_result[0]["values"]:
                    result["network_out"].append(
                        {
                            "timestamp": ts,
                            "value": round(float(value), 2),
                        }
                    )

            # Disk read (bytes/sec, convert to KB/s)
            disk_read_query = (
                f"sum(rate(container_fs_reads_bytes_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f"}}[{step}])) / 1024"
            )
            disk_read_result = self.prom.custom_query_range(
                query=disk_read_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if disk_read_result and len(disk_read_result) > 0 and "values" in disk_read_result[0]:
                for ts, value in disk_read_result[0]["values"]:
                    result["disk_read"].append(
                        {
                            "timestamp": ts,
                            "value": round(float(value), 2),
                        }
                    )

            # Disk write (bytes/sec, convert to KB/s)
            disk_write_query = (
                f"sum(rate(container_fs_writes_bytes_total{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f"}}[{step}])) / 1024"
            )
            disk_write_result = self.prom.custom_query_range(
                query=disk_write_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if disk_write_result and len(disk_write_result) > 0 and "values" in disk_write_result[0]:
                for ts, value in disk_write_result[0]["values"]:
                    result["disk_write"].append(
                        {
                            "timestamp": ts,
                            "value": round(float(value), 2),
                        }
                    )

            # Extract timestamps for each metric (will be converted to local time in browser)
            result["cpu_timestamps"] = [item["timestamp"] for item in result["cpu"]]
            result["memory_timestamps"] = [item["timestamp"] for item in result["memory"]]
            result["network_timestamps"] = [item["timestamp"] for item in result["network_in"]]
            result["disk_timestamps"] = [item["timestamp"] for item in result["disk_read"]]

            # Fetch resource limits (current values, not time-series)
            # Use max() instead of sum() so multiple pods (e.g. during rollout
            # or CrashLoopBackOff) don't inflate the displayed limit.
            # CPU limit in cores, convert to millicores
            cpu_limit_query = (
                f"max(kube_pod_container_resource_limits{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="cpu"'
                f"}})"
            )
            cpu_limit_result = self.prom.custom_query(cpu_limit_query)
            if cpu_limit_result and len(cpu_limit_result) > 0:
                cpu_limit_cores = float(cpu_limit_result[0]["value"][1])
                result["cpu_limit"] = round(cpu_limit_cores * 1000, 0)  # Convert to millicores

            # Memory limit in bytes, convert to MB
            memory_limit_query = (
                f"max(kube_pod_container_resource_limits{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="memory"'
                f"}})"
            )
            memory_limit_result = self.prom.custom_query(memory_limit_query)
            if memory_limit_result and len(memory_limit_result) > 0:
                memory_limit_bytes = float(memory_limit_result[0]["value"][1])
                result["memory_limit"] = round(memory_limit_bytes / (1024 * 1024), 0)  # Convert to MB

            # Memory request in bytes, convert to MB (shown on the chart when it
            # differs from the limit — the tuner adjusts requests, not limits)
            memory_request_query = (
                f"max(kube_pod_container_resource_requests{{"
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="memory"'
                f"}})"
            )
            memory_request_result = self.prom.custom_query(memory_request_query)
            if memory_request_result and len(memory_request_result) > 0:
                memory_request_bytes = float(memory_request_result[0]["value"][1])
                result["memory_request"] = round(memory_request_bytes / (1024 * 1024), 0)  # Convert to MB

        except Exception as e:
            logger.warning(f"Failed to get time-series metrics for {namespace}/{pod_prefix}: {e}")

        return result

    async def get_deployment_component_metrics_timeseries(
        self,
        namespace: str,
        components: list[str],
        deployment_name: str,
        duration_minutes: int = 60,
        step_minutes: int = 5,
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """
        Get time-series metrics for all components in a deployment.

        Args:
            namespace: The Kubernetes namespace
            components: List of component names (component references)
            deployment_name: Name of the deployment
            duration_minutes: How far back to query (default: 60 minutes)
            step_minutes: Interval between data points (default: 5 minutes)

        Returns:
            Dictionary mapping component names to their time-series metrics
        """
        pod_prefixes = {name: generate_unique_name(deployment_name, name) for name in components}

        with ThreadPoolExecutor(max_workers=len(pod_prefixes)) as executor:
            futures = {
                name: executor.submit(
                    self.get_component_metrics_timeseries, namespace, prefix, duration_minutes, step_minutes
                )
                for name, prefix in pod_prefixes.items()
            }
            return {name: future.result() for name, future in futures.items()}

    async def get_pvc_storage_by_namespace(
        self, namespace: str, duration_minutes: int = 60, step_minutes: int = 5
    ) -> dict[str, dict[str, Any]]:
        """
        Get PVC storage usage time-series for all PVCs in a namespace.

        This method queries all PVCs in a namespace without any name pattern matching,
        making it simple and reliable across different environments.

        Args:
            namespace: The Kubernetes namespace
            duration_minutes: How far back to query (default: 60 minutes)
            step_minutes: Interval between data points (default: 5 minutes)

        Returns:
            Dictionary mapping PVC names to their storage data:
            {
                "pvc-name": {
                    "values": [{"timestamp": ts, "value": gb}, ...],
                    "timestamps": [ts, ...],
                    "capacity_gb": float,
                    "warning_threshold_gb": float,  # 80% of capacity
                    "critical_threshold_gb": float,  # 90% of capacity
                }
            }
        """
        from datetime import datetime, timedelta

        if not PrometheusConnector.is_connected:
            return {}

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(minutes=duration_minutes)
        step = f"{step_minutes}m"

        result: dict[str, dict[str, Any]] = {}

        try:
            # Query all PVC usage in the namespace - simple namespace filter only
            pvc_used_query = f'kubelet_volume_stats_used_bytes{{namespace="{namespace}"}}'
            pvc_used_result = self.prom.custom_query_range(
                query=pvc_used_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            # Get PVC capacities (current values)
            pvc_capacity_query = f'kubelet_volume_stats_capacity_bytes{{namespace="{namespace}"}}'
            pvc_capacity_result: list[dict[str, Any]] = self.prom.custom_query(pvc_capacity_query)

            # Build capacity lookup by PVC name
            pvc_capacities: dict[str, float] = {}
            for item in pvc_capacity_result:
                pvc_name = item.get("metric", {}).get("persistentvolumeclaim", "")
                if pvc_name:
                    capacity_bytes = float(item["value"][1])
                    pvc_capacities[pvc_name] = capacity_bytes

            # Process time-series data for each PVC
            if pvc_used_result:
                for series in pvc_used_result:
                    pvc_name = series.get("metric", {}).get("persistentvolumeclaim", "")
                    if not pvc_name or "values" not in series:
                        continue

                    capacity_bytes = pvc_capacities.get(pvc_name, 0)
                    capacity_gb = capacity_bytes / (1024 * 1024 * 1024)

                    pvc_data: dict[str, Any] = {
                        "values": [],
                        "timestamps": [],
                        "capacity_gb": round(capacity_gb, 2),
                        "warning_threshold_gb": round(capacity_gb * 0.8, 2),
                        "critical_threshold_gb": round(capacity_gb * 0.9, 2),
                    }

                    for ts, value in series["values"]:
                        used_gb = float(value) / (1024 * 1024 * 1024)
                        pvc_data["values"].append({"timestamp": ts, "value": round(used_gb, 3)})
                        pvc_data["timestamps"].append(ts)

                    result[pvc_name] = pvc_data

        except Exception as e:
            logger.warning(f"Failed to get PVC storage metrics for namespace {namespace}: {e}")

        return result

    async def discover_workloads_in_namespace(self, namespace: str) -> list[dict[str, Any]]:
        """
        Discover workloads (deployments/statefulsets) in a namespace via Prometheus metrics.

        Uses kube_pod_info to find pods and groups them by their owner (deployment/statefulset).
        Filters out Jobs as they are typically short-lived and don't need ongoing monitoring.
        This is useful for helm-based deployments where we don't know the component structure upfront.

        Args:
            namespace: The Kubernetes namespace to discover workloads in

        Returns:
            List of workload dictionaries with:
            - name: Workload name (deployment/statefulset name)
            - pod_count: Number of pods for this workload
            - pods: List of pod names
            - workload_type: Type of workload (Deployment, StatefulSet)
        """
        if not PrometheusConnector.is_connected:
            logger.warning("Prometheus not connected, cannot discover workloads")
            return []

        workloads: dict[str, dict[str, Any]] = {}

        try:
            # Query kube_pod_info to get all pods in the namespace
            # The created_by_kind label tells us if it's a ReplicaSet (Deployment), StatefulSet, or Job
            query = f'kube_pod_info{{namespace="{namespace}"}}'
            logger.debug(f"Discovering workloads with query: {query}")

            result: list[dict[str, Any]] = self.prom.custom_query(query)

            for item in result:
                metric = item.get("metric", {})
                pod_name = metric.get("pod", "")
                created_by_kind = metric.get("created_by_kind", "")

                if not pod_name:
                    continue

                # Skip Jobs - they are short-lived and don't need ongoing monitoring
                if created_by_kind == "Job":
                    continue

                # Determine workload type
                if created_by_kind == "StatefulSet":
                    workload_type = "StatefulSet"
                elif created_by_kind == "ReplicaSet":
                    workload_type = "Deployment"
                elif created_by_kind == "DaemonSet":
                    workload_type = "DaemonSet"
                else:
                    # Unknown type, skip to be safe (could be a Job without label)
                    workload_type = created_by_kind or "Unknown"
                    # If no created_by_kind, try to infer from pod name pattern
                    if not created_by_kind:
                        # Skip if it looks like a job pod (single hash suffix)
                        parts = pod_name.split("-")
                        if (
                            len(parts) >= 2
                            and len(parts[-1]) >= 5
                            and parts[-1].isalnum()
                            and not (parts[-1].isdigit() or (len(parts) >= 3 and len(parts[-2]) >= 5))
                        ):
                            # Second-to-last is NOT a hash (StatefulSet ordinal or Deployment pattern)
                            continue

                # Extract workload name from pod name
                workload_name = self._extract_workload_name_from_pod(pod_name)

                if workload_name not in workloads:
                    workloads[workload_name] = {
                        "name": workload_name,
                        "pod_count": 0,
                        "pods": [],
                        "workload_type": workload_type,
                    }

                workloads[workload_name]["pod_count"] += 1
                workloads[workload_name]["pods"].append(pod_name)

            # Sort by workload name for consistent ordering
            return sorted(workloads.values(), key=lambda w: w["name"])

        except Exception as e:
            logger.warning(f"Failed to discover workloads in namespace {namespace}: {e}")
            return []

    def _extract_workload_name_from_pod(self, pod_name: str) -> str:
        """
        Extract the workload name from a pod name.

        Handles common Kubernetes pod naming patterns:
        - Deployment pods: {deployment}-{replicaset-hash}-{pod-hash}
        - StatefulSet pods: {statefulset}-{ordinal}
        - Job pods: {job}-{hash}

        Args:
            pod_name: The full pod name

        Returns:
            The extracted workload name
        """
        parts = pod_name.split("-")

        if len(parts) < 2:
            return pod_name

        # Check if last part is a number (StatefulSet ordinal)
        if parts[-1].isdigit():
            # StatefulSet: name-0, name-1, etc.
            return "-".join(parts[:-1])

        # Check for deployment pattern: name-replicaset-hash-pod-hash
        # ReplicaSet hash is typically 9-10 alphanumeric chars
        # Pod hash is typically 5 alphanumeric chars
        if len(parts) >= 3:
            last_part = parts[-1]
            second_last = parts[-2]

            # Deployment pattern: last two parts are hashes
            if len(last_part) == 5 and last_part.isalnum() and len(second_last) >= 5 and second_last.isalnum():
                return "-".join(parts[:-2])

        # Job pattern or unknown: just remove last hash-like part
        if len(parts[-1]) >= 5 and parts[-1].isalnum():
            return "-".join(parts[:-1])

        return pod_name

    async def get_discovered_workload_metrics_timeseries(
        self,
        namespace: str,
        workloads: list[dict[str, Any]],
        duration_minutes: int = 60,
        step_minutes: int = 5,
    ) -> dict[str, dict[str, Any]]:
        """
        Get time-series metrics for discovered workloads in a namespace.

        Similar to get_deployment_component_metrics_timeseries but works with
        dynamically discovered workloads instead of predefined components.

        Args:
            namespace: The Kubernetes namespace
            workloads: List of workload dicts from discover_workloads_in_namespace()
            duration_minutes: How far back to query (default: 60 minutes)
            step_minutes: Interval between data points (default: 5 minutes)

        Returns:
            Dictionary mapping workload names to their time-series metrics
        """
        names = [w.get("name", "") for w in workloads if w.get("name")]

        with ThreadPoolExecutor(max_workers=len(names) or 1) as executor:
            futures = {
                name: executor.submit(
                    self.get_component_metrics_timeseries, namespace, name, duration_minutes, step_minutes
                )
                for name in names
            }
            return {name: future.result() for name, future in futures.items()}


def create_prometheus_connector() -> PrometheusConnector:
    """
    Create and return a PrometheusConnector instance.

    Returns:
        PrometheusConnector instance (singleton)
    """
    logger.debug("Creating PrometheusConnector")
    return PrometheusConnector()


async def get_metrics_connector() -> Any:
    """
    Get the appropriate metrics connector based on configuration.

    Returns PrometheusConnector for direct Prometheus access (local/dev)
    or GrafanaPrometheusConnector for Grafana API access (ODCN production).

    Both connectors implement the same interface with methods like:
    - custom_query(query) -> list[dict]
    - get_cpu_usage_by_namespace(namespace) -> list[dict]
    - get_memory_usage_by_namespace(namespace) -> list[dict]
    - get_cluster_overview() -> dict
    - etc.

    Returns:
        Metrics connector instance (singleton) - either PrometheusConnector
        or GrafanaPrometheusConnector depending on METRICS_BACKEND setting.
    """
    from opi.core.config import settings

    if settings.METRICS_BACKEND == "grafana":
        from opi.connectors.grafana_prometheus import create_grafana_prometheus_connector

        logger.info("Using Grafana-based metrics connector (METRICS_BACKEND=grafana)")
        return await create_grafana_prometheus_connector()
    else:
        logger.info("Using direct Prometheus connector (METRICS_BACKEND=prometheus)")
        return create_prometheus_connector()
