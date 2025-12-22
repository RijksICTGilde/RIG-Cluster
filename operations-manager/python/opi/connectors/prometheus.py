"""
Prometheus connector for querying metrics from Prometheus.

This module provides functionality to interact with Prometheus for retrieving
cluster and application metrics.
"""

import logging
from datetime import UTC
from typing import Any

from prometheus_api_client import PrometheusConnect  # type: ignore[import-untyped]

from opi.core.config import settings
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


class PrometheusConnectionError(Exception):
    """Exception raised when Prometheus connection fails."""


class PrometheusQueryError(Exception):
    """Exception raised when a Prometheus query fails."""


class PrometheusConnector:
    """Connector for interacting with Prometheus for metrics retrieval."""

    _instance: "PrometheusConnector | None" = None
    is_connected: bool = False
    prom: Any  # PrometheusConnect instance (untyped library)
    _initialized: bool
    _prometheus_url: str

    def __new__(cls) -> "PrometheusConnector":
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the Prometheus connector."""
        if self._initialized:
            return

        self._initialized = True
        self._prometheus_url = getattr(settings, "PROMETHEUS_URL", "http://prometheus.rig-system:9090")
        logger.debug(f"Initializing PrometheusConnector with URL: {self._prometheus_url}")

        self.prom = PrometheusConnect(url=self._prometheus_url, disable_ssl=True)
        self._test_connection()

        logger.debug("PrometheusConnector initialized")

    def _test_connection(self) -> bool:
        """
        Test the connection to Prometheus.

        Returns:
            True if connection is successful, False otherwise.
        """
        try:
            result: list[dict[str, Any]] = self.prom.custom_query("prometheus_build_info")
            if result:
                PrometheusConnector.is_connected = True
                logger.info("Prometheus connection successful")
                return True
            else:
                PrometheusConnector.is_connected = False
                logger.warning("Prometheus connection test returned empty result")
                return False
        except Exception as e:
            PrometheusConnector.is_connected = False
            logger.warning(f"Prometheus connection failed: {e}")
            return False

    def reconnect(self) -> bool:
        """
        Attempt to reconnect to Prometheus.

        This method can be called to retry the connection if the initial
        connection failed (e.g., Prometheus wasn't ready at startup).

        Returns:
            True if reconnection is successful, False otherwise.
        """
        if PrometheusConnector.is_connected:
            logger.debug("Prometheus already connected, skipping reconnect")
            return True

        logger.info(f"Attempting to reconnect to Prometheus at {self._prometheus_url}")
        return self._test_connection()

    def _ensure_connected(self) -> None:
        """Ensure Prometheus is connected, raise error if not."""
        if not PrometheusConnector.is_connected:
            raise PrometheusConnectionError("Prometheus is not connected")

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
            query = f'sum(container_memory_usage_bytes{{namespace="{namespace}",container!=""}}) by (pod)'
        else:
            query = 'sum(container_memory_usage_bytes{container!=""}) by (namespace, pod)'

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

        if namespace:
            query = f'count(kube_pod_info{{namespace="{namespace}"}})'
        else:
            query = "count(kube_pod_info)"

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
                'sum(container_memory_usage_bytes{container!=""})'
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

    def custom_query(self, query: str) -> list[dict[str, Any]]:
        """
        Execute a custom PromQL query.

        Args:
            query: The PromQL query to execute.

        Returns:
            List of metric results.
        """
        self._ensure_connected()

        logger.debug(f"Executing custom query: {query}")

        try:
            result: list[dict[str, Any]] = self.prom.custom_query(query)
            return result
        except Exception as e:
            logger.error(f"Failed to execute custom query: {e}")
            raise PrometheusQueryError(f"Failed to execute custom query: {e}") from e

    def query_range(
        self, query: str, start_time: str, end_time: str, step: str
    ) -> list[dict[str, Any]]:
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

    def get_component_metrics(
        self, namespace: str, pod_prefix: str, time_range: str = "6h"
    ) -> dict[str, float | None]:
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
                f'avg(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}}[{time_range}]))'
            )
            cpu_result: list[dict[str, Any]] = self.prom.custom_query(cpu_query)
            if cpu_result and len(cpu_result) > 0:
                metrics["cpu_cores"] = float(cpu_result[0]["value"][1])

            # Memory usage (current, in bytes)
            memory_query = (
                f'sum(container_memory_usage_bytes{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}})'
            )
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
                req_query = (
                    f'sum(rate({req_metric}{{'
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                    f'}}[{time_range}]))'
                )
                try:
                    req_result: list[dict[str, Any]] = self.prom.custom_query(req_query)
                    if req_result and len(req_result) > 0:
                        value = float(req_result[0]["value"][1])
                        if value > 0:
                            metrics["requests_per_second"] = value
                            break
                except Exception:
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
        result: dict[str, dict[str, float | None]] = {}

        for component_name in components:
            # Pod names follow pattern: {deployment_name}-{component_name}-{hash}-{hash}
            # Use generate_unique_name to construct the Kubernetes resource name
            pod_prefix = generate_unique_name(deployment_name, component_name)
            result[component_name] = self.get_component_metrics(namespace, pod_prefix, time_range)

        return result

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
                "cpu": [], "memory": [], "requests": [],
                "network_in": [], "network_out": [], "disk_read": [], "disk_write": [],
                "cpu_limit": None, "memory_limit": None,
                "cpu_timestamps": [], "memory_timestamps": [], "requests_timestamps": [],
                "network_timestamps": [], "disk_timestamps": [],
            }

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(minutes=duration_minutes)
        step = f"{step_minutes}m"

        result: dict[str, Any] = {
            "cpu": [], "memory": [], "requests": [],
            "network_in": [], "network_out": [], "disk_read": [], "disk_write": [],
            "cpu_limit": None, "memory_limit": None,
            "cpu_timestamps": [], "memory_timestamps": [], "requests_timestamps": [],
            "network_timestamps": [], "disk_timestamps": [],
        }

        try:
            # CPU usage time-series (in cores, we'll convert to millicores)
            cpu_query = (
                f'sum(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}}[{step}]))'
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
                    result["cpu"].append({
                        "timestamp": ts,
                        "value": round(cpu_millicores, 2),
                    })

            # Memory usage time-series (in bytes, we'll convert to MB)
            memory_query = (
                f'sum(container_memory_usage_bytes{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}})'
            )
            memory_result = self.prom.custom_query_range(
                query=memory_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if memory_result and len(memory_result) > 0 and "values" in memory_result[0]:
                for ts, value in memory_result[0]["values"]:
                    memory_mb = float(value) / (1024 * 1024)  # Convert to MB
                    result["memory"].append({
                        "timestamp": ts,
                        "value": round(memory_mb, 1),
                    })

            # HTTP requests per minute time-series (rate * 60 for better readability)
            requests_query = (
                f'sum(rate(http_requests_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) * 60'
            )
            requests_result = self.prom.custom_query_range(
                query=requests_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            logger.debug(f"Prometheus requests result: {requests_result}")
            if requests_result and len(requests_result) > 0 and "values" in requests_result[0]:
                for ts, value in requests_result[0]["values"]:
                    rpm = float(value)  # requests per minute
                    result["requests"].append({
                        "timestamp": ts,
                        "value": round(rpm, 1),
                    })
                logger.debug(f"Processed {len(result['requests'])} request data points")

            # Network receive (bytes/sec, convert to KB/s)
            network_in_query = (
                f'sum(rate(container_network_receive_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            network_in_result = self.prom.custom_query_range(
                query=network_in_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if network_in_result and len(network_in_result) > 0 and "values" in network_in_result[0]:
                for ts, value in network_in_result[0]["values"]:
                    result["network_in"].append({
                        "timestamp": ts,
                        "value": round(float(value), 2),
                    })

            # Network transmit (bytes/sec, convert to KB/s)
            network_out_query = (
                f'sum(rate(container_network_transmit_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            network_out_result = self.prom.custom_query_range(
                query=network_out_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if network_out_result and len(network_out_result) > 0 and "values" in network_out_result[0]:
                for ts, value in network_out_result[0]["values"]:
                    result["network_out"].append({
                        "timestamp": ts,
                        "value": round(float(value), 2),
                    })

            # Disk read (bytes/sec, convert to KB/s)
            disk_read_query = (
                f'sum(rate(container_fs_reads_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            disk_read_result = self.prom.custom_query_range(
                query=disk_read_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if disk_read_result and len(disk_read_result) > 0 and "values" in disk_read_result[0]:
                for ts, value in disk_read_result[0]["values"]:
                    result["disk_read"].append({
                        "timestamp": ts,
                        "value": round(float(value), 2),
                    })

            # Disk write (bytes/sec, convert to KB/s)
            disk_write_query = (
                f'sum(rate(container_fs_writes_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            disk_write_result = self.prom.custom_query_range(
                query=disk_write_query,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            if disk_write_result and len(disk_write_result) > 0 and "values" in disk_write_result[0]:
                for ts, value in disk_write_result[0]["values"]:
                    result["disk_write"].append({
                        "timestamp": ts,
                        "value": round(float(value), 2),
                    })

            # Extract timestamps for each metric (will be converted to local time in browser)
            result["cpu_timestamps"] = [item["timestamp"] for item in result["cpu"]]
            result["memory_timestamps"] = [item["timestamp"] for item in result["memory"]]
            result["requests_timestamps"] = [item["timestamp"] for item in result["requests"]]
            result["network_timestamps"] = [item["timestamp"] for item in result["network_in"]]
            result["disk_timestamps"] = [item["timestamp"] for item in result["disk_read"]]

            # Fetch resource limits (current values, not time-series)
            # CPU limit in cores, convert to millicores
            cpu_limit_query = (
                f'sum(kube_pod_container_resource_limits{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="cpu"'
                f'}})'
            )
            cpu_limit_result = self.prom.custom_query(cpu_limit_query)
            if cpu_limit_result and len(cpu_limit_result) > 0:
                cpu_limit_cores = float(cpu_limit_result[0]["value"][1])
                result["cpu_limit"] = round(cpu_limit_cores * 1000, 0)  # Convert to millicores

            # Memory limit in bytes, convert to MB
            memory_limit_query = (
                f'sum(kube_pod_container_resource_limits{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="memory"'
                f'}})'
            )
            memory_limit_result = self.prom.custom_query(memory_limit_query)
            if memory_limit_result and len(memory_limit_result) > 0:
                memory_limit_bytes = float(memory_limit_result[0]["value"][1])
                result["memory_limit"] = round(memory_limit_bytes / (1024 * 1024), 0)  # Convert to MB

        except Exception as e:
            logger.warning(f"Failed to get time-series metrics for {namespace}/{pod_prefix}: {e}")

        return result

    def get_deployment_component_metrics_timeseries(
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
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for component_name in components:
            # Pod names follow pattern: {deployment_name}-{component_name}-{hash}-{hash}
            # Use generate_unique_name to construct the Kubernetes resource name
            pod_prefix = generate_unique_name(deployment_name, component_name)
            result[component_name] = self.get_component_metrics_timeseries(
                namespace, pod_prefix, duration_minutes, step_minutes
            )

        return result


def create_prometheus_connector() -> PrometheusConnector:
    """
    Create and return a PrometheusConnector instance.

    Returns:
        PrometheusConnector instance (singleton)
    """
    logger.debug("Creating PrometheusConnector")
    return PrometheusConnector()
