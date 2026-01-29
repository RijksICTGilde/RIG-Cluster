"""
Grafana Prometheus connector for querying metrics via Grafana's API.

This module provides functionality to query Prometheus/Mimir metrics through
Grafana's datasource API, using a service account token for authentication.
This is used in ODCN production where direct Prometheus access is not available.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from opi.core.config import settings
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


class GrafanaConnectionError(Exception):
    """Exception raised when Grafana connection fails."""


class GrafanaQueryError(Exception):
    """Exception raised when a Grafana query fails."""


class GrafanaPrometheusConnector:
    """
    Connector for querying Prometheus metrics through Grafana's API.

    This connector provides the same interface as PrometheusConnector but
    queries metrics via Grafana's /api/ds/query endpoint instead of directly
    accessing Prometheus.
    """

    _instance: "GrafanaPrometheusConnector | None" = None
    is_connected: bool = False
    _initialized: bool
    _grafana_url: str
    _token: str | None
    _datasource_uid: str | None
    _datasource_type: str

    def __new__(cls) -> "GrafanaPrometheusConnector":
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the Grafana Prometheus connector."""
        if self._initialized:
            return

        self._initialized = True
        self._grafana_url = settings.GRAFANA_URL.rstrip("/")
        self._token = settings.GRAFANA_TOKEN
        self._datasource_uid = settings.GRAFANA_DATASOURCE_UID
        self._datasource_type = "prometheus"  # Default, will be updated on discovery

        logger.debug(f"Initializing GrafanaPrometheusConnector with URL: {self._grafana_url}")

        if not self._token:
            logger.warning("GRAFANA_TOKEN not set - Grafana connector will not work")
            GrafanaPrometheusConnector.is_connected = False
            return

        # Auto-discover datasource if not configured
        if not self._datasource_uid:
            self._discover_datasource()

        self._test_connection()
        logger.debug("GrafanaPrometheusConnector initialized")

    def _get_headers(self) -> dict[str, str]:
        """Get HTTP headers with Bearer token authentication."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _discover_datasource(self) -> None:
        """Auto-discover the Prometheus/Mimir datasource UID."""
        logger.debug("Auto-discovering Prometheus/Mimir datasource...")

        try:
            url = f"{self._grafana_url}/api/datasources"
            resp = requests.get(url, headers=self._get_headers(), timeout=10)

            if resp.status_code != 200:
                logger.warning(f"Failed to fetch datasources: {resp.status_code}")
                return

            datasources = resp.json()
            for ds in datasources:
                ds_type = ds.get("type", "")
                if ds_type in ("prometheus", "mimir"):
                    self._datasource_uid = ds.get("uid")
                    self._datasource_type = ds_type
                    logger.info(
                        f"Auto-discovered datasource: {ds.get('name')} "
                        f"(type: {ds_type}, uid: {self._datasource_uid})"
                    )
                    return

            logger.warning("No Prometheus/Mimir datasource found in Grafana")

        except requests.RequestException as e:
            logger.warning(f"Error discovering datasource: {e}")

    def _test_connection(self) -> bool:
        """Test the connection to Grafana and the Prometheus datasource."""
        if not self._datasource_uid:
            logger.warning("No datasource UID configured - cannot test connection")
            GrafanaPrometheusConnector.is_connected = False
            return False

        try:
            # Test with a simple query
            result = self._execute_query("up", instant=True)
            if result is not None:
                GrafanaPrometheusConnector.is_connected = True
                logger.info("Grafana Prometheus connection successful")
                return True
            else:
                GrafanaPrometheusConnector.is_connected = False
                logger.warning("Grafana connection test returned no result")
                return False

        except Exception as e:
            GrafanaPrometheusConnector.is_connected = False
            logger.warning(f"Grafana connection failed: {e}")
            return False

    def reconnect(self) -> bool:
        """Attempt to reconnect to Grafana."""
        if GrafanaPrometheusConnector.is_connected:
            logger.debug("Grafana already connected, skipping reconnect")
            return True

        logger.info(f"Attempting to reconnect to Grafana at {self._grafana_url}")

        if not self._datasource_uid:
            self._discover_datasource()

        return self._test_connection()

    def _ensure_connected(self) -> None:
        """Ensure Grafana is connected, raise error if not."""
        if not GrafanaPrometheusConnector.is_connected:
            raise GrafanaConnectionError("Grafana is not connected")

    def _execute_query(
        self,
        query: str,
        instant: bool = True,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        step: str = "5m",
    ) -> list[dict[str, Any]]:
        """
        Execute a PromQL query through Grafana's API.

        Args:
            query: The PromQL query to execute
            instant: If True, execute instant query. If False, execute range query.
            start_time: Start time for range queries
            end_time: End time for range queries
            step: Step interval for range queries

        Returns:
            List of metric results in Prometheus API format
        """
        url = f"{self._grafana_url}/api/ds/query"

        query_obj: dict[str, Any] = {
            "refId": "A",
            "datasource": {
                "type": self._datasource_type,
                "uid": self._datasource_uid,
            },
            "expr": query,
            "instant": instant,
            "range": not instant,
        }

        if not instant and step:
            query_obj["interval"] = step

        payload: dict[str, Any] = {
            "queries": [query_obj],
            "from": "now-5m",
            "to": "now",
        }

        # For range queries, use specific timestamps
        if not instant and start_time and end_time:
            payload["from"] = str(int(start_time.timestamp() * 1000))
            payload["to"] = str(int(end_time.timestamp() * 1000))

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=30)

            if resp.status_code != 200:
                logger.error(f"Grafana query failed: {resp.status_code} - {resp.text[:500]}")
                raise GrafanaQueryError(f"Query failed with status {resp.status_code}")

            result = resp.json()
            return self._convert_grafana_response(result, instant)

        except requests.RequestException as e:
            logger.error(f"Grafana request error: {e}")
            raise GrafanaQueryError(f"Request failed: {e}") from e

    def _convert_grafana_response(
        self, grafana_result: dict[str, Any], instant: bool = True
    ) -> list[dict[str, Any]]:
        """
        Convert Grafana API response to Prometheus API format.

        Grafana returns data in a different format than the Prometheus API.
        This method converts it to match the prometheus_api_client format.
        """
        results: list[dict[str, Any]] = []

        ref_results = grafana_result.get("results", {})
        for ref_result in ref_results.values():
            frames = ref_result.get("frames", [])

            for frame in frames:
                schema = frame.get("schema", {})
                data = frame.get("data", {})

                # Extract labels from schema fields
                labels: dict[str, str] = {}
                fields = schema.get("fields", [])
                for field in fields:
                    field_labels = field.get("labels", {})
                    if field_labels:
                        labels.update(field_labels)

                # Get values
                values = data.get("values", [])

                if instant:
                    # Instant query: single value
                    if values and len(values) > 1 and values[1]:
                        timestamp = values[0][0] if values[0] else 0
                        value = values[1][0] if values[1] else 0
                        results.append({
                            "metric": labels,
                            "value": [timestamp / 1000, str(value)],  # Convert ms to seconds
                        })
                else:
                    # Range query: multiple values over time
                    if values and len(values) > 1:
                        timestamps = values[0] if values[0] else []
                        metric_values = values[1] if values[1] else []

                        range_values = []
                        for i, ts in enumerate(timestamps):
                            if i < len(metric_values):
                                range_values.append([ts / 1000, str(metric_values[i])])

                        if range_values:
                            results.append({
                                "metric": labels,
                                "values": range_values,
                            })

        return results

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
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to execute custom query: {e}")
            raise GrafanaQueryError(f"Failed to execute custom query: {e}") from e

    def get_cpu_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get CPU usage metrics grouped by pod."""
        self._ensure_connected()

        if namespace:
            query = (
                f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",container!=""}}[5m])) by (pod)'
            )
        else:
            query = 'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m])) by (namespace, pod)'

        logger.debug(f"Querying CPU usage: {query}")

        try:
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query CPU usage: {e}")
            raise GrafanaQueryError(f"Failed to query CPU usage: {e}") from e

    def get_memory_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get memory usage metrics grouped by pod."""
        self._ensure_connected()

        if namespace:
            query = f'sum(container_memory_usage_bytes{{namespace="{namespace}",container!=""}}) by (pod)'
        else:
            query = 'sum(container_memory_usage_bytes{container!=""}) by (namespace, pod)'

        logger.debug(f"Querying memory usage: {query}")

        try:
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query memory usage: {e}")
            raise GrafanaQueryError(f"Failed to query memory usage: {e}") from e

    def get_pod_count(self, namespace: str | None = None) -> int:
        """Get the count of running pods."""
        self._ensure_connected()

        if namespace:
            query = f'count(kube_pod_info{{namespace="{namespace}"}})'
        else:
            query = "count(kube_pod_info)"

        logger.debug(f"Querying pod count: {query}")

        try:
            result = self._execute_query(query, instant=True)
            if result and len(result) > 0:
                return int(float(result[0]["value"][1]))
            return 0
        except Exception as e:
            logger.error(f"Failed to query pod count: {e}")
            raise GrafanaQueryError(f"Failed to query pod count: {e}") from e

    def get_pod_restarts(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get pod restart counts."""
        self._ensure_connected()

        if namespace:
            query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}) by (pod)'
        else:
            query = "sum(kube_pod_container_status_restarts_total) by (namespace, pod)"

        logger.debug(f"Querying pod restarts: {query}")

        try:
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query pod restarts: {e}")
            raise GrafanaQueryError(f"Failed to query pod restarts: {e}") from e

    def get_network_receive_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get network receive bytes rate."""
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_receive_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_receive_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network receive: {query}")

        try:
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query network receive: {e}")
            raise GrafanaQueryError(f"Failed to query network receive: {e}") from e

    def get_network_transmit_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get network transmit bytes rate."""
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_transmit_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network transmit: {query}")

        try:
            return self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query network transmit: {e}")
            raise GrafanaQueryError(f"Failed to query network transmit: {e}") from e

    def get_cluster_overview(self) -> dict[str, Any]:
        """Get a high-level overview of cluster metrics."""
        self._ensure_connected()

        overview = {
            "total_cpu_usage": 0.0,
            "total_memory_bytes": 0,
            "pod_count": 0,
            "namespace_count": 0,
        }

        try:
            # Total CPU usage
            cpu_result = self._execute_query(
                'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m]))',
                instant=True,
            )
            if cpu_result:
                overview["total_cpu_usage"] = float(cpu_result[0]["value"][1])

            # Total memory usage
            memory_result = self._execute_query(
                'sum(container_memory_usage_bytes{container!=""})',
                instant=True,
            )
            if memory_result:
                overview["total_memory_bytes"] = int(float(memory_result[0]["value"][1]))

            # Pod count
            pod_result = self._execute_query("count(kube_pod_info)", instant=True)
            if pod_result:
                overview["pod_count"] = int(float(pod_result[0]["value"][1]))

            # Namespace count
            ns_result = self._execute_query(
                "count(count by (namespace) (kube_pod_info))",
                instant=True,
            )
            if ns_result:
                overview["namespace_count"] = int(float(ns_result[0]["value"][1]))

        except Exception as e:
            logger.error(f"Failed to get cluster overview: {e}")
            raise GrafanaQueryError(f"Failed to get cluster overview: {e}") from e

        return overview

    def query_range(
        self, query: str, start_time: str, end_time: str, step: str
    ) -> list[dict[str, Any]]:
        """Execute a range query to get time-series data."""
        self._ensure_connected()

        logger.debug(f"Executing range query: {query} from {start_time} to {end_time} step {step}")

        try:
            # Parse time strings to datetime
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))

            return self._execute_query(
                query,
                instant=False,
                start_time=start_dt,
                end_time=end_dt,
                step=step,
            )
        except Exception as e:
            logger.error(f"Failed to execute range query: {e}")
            raise GrafanaQueryError(f"Failed to execute range query: {e}") from e

    def get_component_metrics(
        self, namespace: str, pod_prefix: str, time_range: str = "6h"
    ) -> dict[str, float | None]:
        """Get aggregated metrics for a specific component."""
        if not GrafanaPrometheusConnector.is_connected:
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
            # CPU usage
            cpu_query = (
                f'avg(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}}[{time_range}]))'
            )
            cpu_result = self._execute_query(cpu_query, instant=True)
            if cpu_result and len(cpu_result) > 0:
                metrics["cpu_cores"] = float(cpu_result[0]["value"][1])

            # Memory usage
            memory_query = (
                f'sum(container_memory_usage_bytes{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}})'
            )
            memory_result = self._execute_query(memory_query, instant=True)
            if memory_result and len(memory_result) > 0:
                memory_bytes = float(memory_result[0]["value"][1])
                metrics["memory_bytes"] = memory_bytes
                metrics["memory_mb"] = memory_bytes / (1024 * 1024)

            # HTTP requests per second
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
                    req_result = self._execute_query(req_query, instant=True)
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
        """Get metrics for all components in a deployment."""
        result: dict[str, dict[str, float | None]] = {}

        for component_name in components:
            pod_prefix = generate_unique_name(deployment_name, component_name)
            result[component_name] = self.get_component_metrics(namespace, pod_prefix, time_range)

        return result

    def get_component_metrics_timeseries(
        self, namespace: str, pod_prefix: str, duration_minutes: int = 60, step_minutes: int = 5
    ) -> dict[str, Any]:
        """Get time-series metrics for a component over a duration."""
        if not GrafanaPrometheusConnector.is_connected:
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
            # CPU usage time-series
            cpu_query = (
                f'sum(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}}[{step}]))'
            )
            cpu_result = self._execute_query(cpu_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if cpu_result and len(cpu_result) > 0 and "values" in cpu_result[0]:
                for ts, value in cpu_result[0]["values"]:
                    cpu_millicores = float(value) * 1000
                    result["cpu"].append({"timestamp": ts, "value": round(cpu_millicores, 2)})

            # Memory usage time-series
            memory_query = (
                f'sum(container_memory_usage_bytes{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                f'}})'
            )
            memory_result = self._execute_query(memory_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if memory_result and len(memory_result) > 0 and "values" in memory_result[0]:
                for ts, value in memory_result[0]["values"]:
                    memory_mb = float(value) / (1024 * 1024)
                    result["memory"].append({"timestamp": ts, "value": round(memory_mb, 1)})

            # HTTP requests time-series
            requests_query = (
                f'sum(rate(http_requests_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) * 60'
            )
            requests_result = self._execute_query(requests_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if requests_result and len(requests_result) > 0 and "values" in requests_result[0]:
                for ts, value in requests_result[0]["values"]:
                    result["requests"].append({"timestamp": ts, "value": round(float(value), 1)})

            # Network receive
            network_in_query = (
                f'sum(rate(container_network_receive_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            network_in_result = self._execute_query(network_in_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if network_in_result and len(network_in_result) > 0 and "values" in network_in_result[0]:
                for ts, value in network_in_result[0]["values"]:
                    result["network_in"].append({"timestamp": ts, "value": round(float(value), 2)})

            # Network transmit
            network_out_query = (
                f'sum(rate(container_network_transmit_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            network_out_result = self._execute_query(network_out_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if network_out_result and len(network_out_result) > 0 and "values" in network_out_result[0]:
                for ts, value in network_out_result[0]["values"]:
                    result["network_out"].append({"timestamp": ts, "value": round(float(value), 2)})

            # Disk read
            disk_read_query = (
                f'sum(rate(container_fs_reads_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            disk_read_result = self._execute_query(disk_read_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if disk_read_result and len(disk_read_result) > 0 and "values" in disk_read_result[0]:
                for ts, value in disk_read_result[0]["values"]:
                    result["disk_read"].append({"timestamp": ts, "value": round(float(value), 2)})

            # Disk write
            disk_write_query = (
                f'sum(rate(container_fs_writes_bytes_total{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                f'}}[{step}])) / 1024'
            )
            disk_write_result = self._execute_query(disk_write_query, instant=False, start_time=start_time, end_time=end_time, step=step)
            if disk_write_result and len(disk_write_result) > 0 and "values" in disk_write_result[0]:
                for ts, value in disk_write_result[0]["values"]:
                    result["disk_write"].append({"timestamp": ts, "value": round(float(value), 2)})

            # Extract timestamps
            result["cpu_timestamps"] = [item["timestamp"] for item in result["cpu"]]
            result["memory_timestamps"] = [item["timestamp"] for item in result["memory"]]
            result["requests_timestamps"] = [item["timestamp"] for item in result["requests"]]
            result["network_timestamps"] = [item["timestamp"] for item in result["network_in"]]
            result["disk_timestamps"] = [item["timestamp"] for item in result["disk_read"]]

            # Fetch resource limits
            cpu_limit_query = (
                f'sum(kube_pod_container_resource_limits{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="cpu"'
                f'}})'
            )
            cpu_limit_result = self._execute_query(cpu_limit_query, instant=True)
            if cpu_limit_result and len(cpu_limit_result) > 0:
                cpu_limit_cores = float(cpu_limit_result[0]["value"][1])
                result["cpu_limit"] = round(cpu_limit_cores * 1000, 0)

            memory_limit_query = (
                f'sum(kube_pod_container_resource_limits{{'
                f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="memory"'
                f'}})'
            )
            memory_limit_result = self._execute_query(memory_limit_query, instant=True)
            if memory_limit_result and len(memory_limit_result) > 0:
                memory_limit_bytes = float(memory_limit_result[0]["value"][1])
                result["memory_limit"] = round(memory_limit_bytes / (1024 * 1024), 0)

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
        """Get time-series metrics for all components in a deployment."""
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for component_name in components:
            pod_prefix = generate_unique_name(deployment_name, component_name)
            result[component_name] = self.get_component_metrics_timeseries(
                namespace, pod_prefix, duration_minutes, step_minutes
            )

        return result

    def discover_workloads_in_namespace(self, namespace: str) -> list[dict[str, Any]]:
        """Discover workloads in a namespace via Prometheus metrics."""
        if not GrafanaPrometheusConnector.is_connected:
            logger.warning("Grafana not connected, cannot discover workloads")
            return []

        workloads: dict[str, dict[str, Any]] = {}

        try:
            query = f'kube_pod_info{{namespace="{namespace}"}}'
            logger.debug(f"Discovering workloads with query: {query}")

            result = self._execute_query(query, instant=True)

            for item in result:
                metric = item.get("metric", {})
                pod_name = metric.get("pod", "")
                created_by_kind = metric.get("created_by_kind", "")

                if not pod_name:
                    continue

                if created_by_kind == "Job":
                    continue

                if created_by_kind == "StatefulSet":
                    workload_type = "StatefulSet"
                elif created_by_kind == "ReplicaSet":
                    workload_type = "Deployment"
                elif created_by_kind == "DaemonSet":
                    workload_type = "DaemonSet"
                else:
                    workload_type = created_by_kind or "Unknown"
                    if not created_by_kind:
                        parts = pod_name.split("-")
                        if len(parts) >= 2 and len(parts[-1]) >= 5 and parts[-1].isalnum():
                            if not (parts[-1].isdigit() or (len(parts) >= 3 and len(parts[-2]) >= 5)):
                                continue

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

            return sorted(workloads.values(), key=lambda w: w["name"])

        except Exception as e:
            logger.warning(f"Failed to discover workloads in namespace {namespace}: {e}")
            return []

    def _extract_workload_name_from_pod(self, pod_name: str) -> str:
        """Extract the workload name from a pod name."""
        parts = pod_name.split("-")

        if len(parts) < 2:
            return pod_name

        if parts[-1].isdigit():
            return "-".join(parts[:-1])

        if len(parts) >= 3:
            last_part = parts[-1]
            second_last = parts[-2]

            if (len(last_part) == 5 and last_part.isalnum() and
                len(second_last) >= 5 and second_last.isalnum()):
                return "-".join(parts[:-2])

        if len(parts[-1]) >= 5 and parts[-1].isalnum():
            return "-".join(parts[:-1])

        return pod_name

    def get_discovered_workload_metrics_timeseries(
        self,
        namespace: str,
        workloads: list[dict[str, Any]],
        duration_minutes: int = 60,
        step_minutes: int = 5,
    ) -> dict[str, dict[str, Any]]:
        """Get time-series metrics for discovered workloads."""
        result: dict[str, dict[str, Any]] = {}

        for workload in workloads:
            workload_name = workload.get("name", "")
            if not workload_name:
                continue

            result[workload_name] = self.get_component_metrics_timeseries(
                namespace, workload_name, duration_minutes, step_minutes
            )

        return result


def create_grafana_prometheus_connector() -> GrafanaPrometheusConnector:
    """Create and return a GrafanaPrometheusConnector instance."""
    logger.debug("Creating GrafanaPrometheusConnector")
    return GrafanaPrometheusConnector()
