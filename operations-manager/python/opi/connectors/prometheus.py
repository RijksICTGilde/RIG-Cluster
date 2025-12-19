"""
Prometheus connector for querying metrics from Prometheus.

This module provides functionality to interact with Prometheus for retrieving
cluster and application metrics.
"""

import logging
from typing import Any

from prometheus_api_client import PrometheusConnect  # type: ignore[import-untyped]

from opi.core.config import settings

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
        prometheus_url = getattr(settings, "PROMETHEUS_URL", "http://prometheus.rig-system:9090")
        logger.debug(f"Initializing PrometheusConnector with URL: {prometheus_url}")

        try:
            self.prom = PrometheusConnect(url=prometheus_url, disable_ssl=True)
            # Test connection by querying Prometheus build info
            result: list[dict[str, Any]] = self.prom.custom_query("prometheus_build_info")
            if result:
                PrometheusConnector.is_connected = True
                logger.info("Prometheus connection successful")
            else:
                PrometheusConnector.is_connected = False
                logger.warning("Prometheus connection test returned empty result")
        except Exception as e:
            PrometheusConnector.is_connected = False
            logger.warning(f"Prometheus connection failed: {e}")

        logger.debug("PrometheusConnector initialized")

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
            components: List of component names
            deployment_name: Name of the deployment (used to construct pod prefix)
            time_range: Time range for rate calculations (default: 6h)

        Returns:
            Dictionary mapping component names to their metrics
        """
        result: dict[str, dict[str, float | None]] = {}

        for component_name in components:
            # Pod names follow pattern: {deployment_name}-{component_name}-*
            pod_prefix = f"{deployment_name}-{component_name}"
            result[component_name] = self.get_component_metrics(namespace, pod_prefix, time_range)

        return result


def create_prometheus_connector() -> PrometheusConnector:
    """
    Create and return a PrometheusConnector instance.

    Returns:
        PrometheusConnector instance (singleton)
    """
    logger.debug("Creating PrometheusConnector")
    return PrometheusConnector()
