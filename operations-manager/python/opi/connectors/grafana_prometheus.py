"""
Grafana Prometheus connector for querying metrics via Grafana's API.

This module provides functionality to query Prometheus/Mimir metrics through
Grafana's datasource API, using a service account token for authentication.
This is used in ODCN production where direct Prometheus access is not available.
"""

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

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
    _client: httpx.AsyncClient | None

    def __new__(cls) -> "GrafanaPrometheusConnector":
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the Grafana Prometheus connector (config only, no I/O)."""
        if self._initialized:
            return

        self._initialized = True
        self._grafana_url = settings.GRAFANA_URL.rstrip("/")
        self._token = settings.GRAFANA_TOKEN
        self._datasource_uid = settings.GRAFANA_DATASOURCE_UID
        self._datasource_type = "prometheus"
        self._client = None

        logger.debug(f"Initializing GrafanaPrometheusConnector with URL: {self._grafana_url}")

        if not self._token:
            logger.warning("GRAFANA_TOKEN not set - Grafana connector will not work")
            GrafanaPrometheusConnector.is_connected = False

    async def connect(self) -> None:
        """Perform async initialization: discover datasource and test connection."""
        if GrafanaPrometheusConnector.is_connected:
            return
        if not self._token:
            return

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))

        if not self._datasource_uid:
            await self._discover_datasource()

        await self._test_connection()
        logger.debug("GrafanaPrometheusConnector initialized")

    def _get_headers(self) -> dict[str, str]:
        """Get HTTP headers with Bearer token authentication."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _discover_datasource(self) -> None:
        """Auto-discover the Prometheus/Mimir datasource UID."""
        if self._client is None:
            return

        logger.debug("Auto-discovering Prometheus/Mimir datasource...")

        try:
            url = f"{self._grafana_url}/api/datasources"
            resp = await self._client.get(url, headers=self._get_headers())

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
                        f"Auto-discovered datasource: {ds.get('name')} (type: {ds_type}, uid: {self._datasource_uid})"
                    )
                    return

            logger.warning("No Prometheus/Mimir datasource found in Grafana")

        except httpx.HTTPError as e:
            logger.warning(f"Error discovering datasource: {e}")

    async def _test_connection(self) -> bool:
        """Test the connection to Grafana and the Prometheus datasource."""
        if not self._datasource_uid:
            logger.warning("No datasource UID configured - cannot test connection")
            GrafanaPrometheusConnector.is_connected = False
            return False

        try:
            result = await self._execute_query("up", instant=True)
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

    async def reconnect(self) -> bool:
        """Attempt to reconnect to Grafana."""
        if GrafanaPrometheusConnector.is_connected:
            logger.debug("Grafana already connected, skipping reconnect")
            return True

        logger.info(f"Attempting to reconnect to Grafana at {self._grafana_url}")

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))

        if not self._datasource_uid:
            await self._discover_datasource()

        return await self._test_connection()

    def _ensure_connected(self) -> None:
        """Ensure Grafana is connected, raise error if not."""
        if not GrafanaPrometheusConnector.is_connected or self._client is None:
            raise GrafanaConnectionError("Grafana is not connected")

    def _build_query_obj(
        self, ref_id: str, query: str, instant: bool, step: str = "5m", datasource_uid: str | None = None
    ) -> dict[str, Any]:
        """Build a single query object for the Grafana API."""
        query_obj: dict[str, Any] = {
            "refId": ref_id,
            "datasource": {
                "type": self._datasource_type,
                "uid": datasource_uid or self._datasource_uid,
            },
            "expr": query,
            "instant": instant,
            "range": not instant,
        }
        if not instant and step:
            query_obj["interval"] = step
        return query_obj

    async def _execute_query(
        self,
        query: str,
        instant: bool = True,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        step: str = "5m",
        datasource_uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a single PromQL query through Grafana's API (async, non-blocking).

        Args:
            query: The PromQL query to execute
            instant: If True, execute instant query. If False, execute range query.
            start_time: Start time for range queries
            end_time: End time for range queries
            step: Step interval for range queries
            datasource_uid: Optional datasource UID override (e.g. for billing Mimir)

        Returns:
            List of metric results in Prometheus API format
        """
        results = await self._execute_batch_queries(
            queries={"A": query},
            instant=instant,
            start_time=start_time,
            end_time=end_time,
            step=step,
            datasource_uid=datasource_uid,
        )
        return results.get("A", [])

    async def _execute_batch_queries(
        self,
        queries: dict[str, str],
        instant: bool = True,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        step: str = "5m",
        datasource_uid: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Execute multiple PromQL queries in a single Grafana API request.

        All queries in a batch share the same instant/range mode and time window.

        Args:
            queries: Mapping of refId to PromQL query string
            instant: If True, execute instant queries. If False, range queries.
            start_time: Start time for range queries
            end_time: End time for range queries
            step: Step interval for range queries
            datasource_uid: Optional datasource UID override (e.g. for billing Mimir)

        Returns:
            Dictionary mapping refId to list of metric results in Prometheus API format
        """
        if self._client is None:
            raise GrafanaConnectionError("HTTP client not initialized")

        if not queries:
            return {}

        url = f"{self._grafana_url}/api/ds/query"

        query_objects = [
            self._build_query_obj(ref_id, query, instant, step, datasource_uid) for ref_id, query in queries.items()
        ]

        payload: dict[str, Any] = {
            "queries": query_objects,
            "from": "now-5m",
            "to": "now",
        }

        if not instant and start_time and end_time:
            payload["from"] = str(int(start_time.timestamp() * 1000))
            payload["to"] = str(int(end_time.timestamp() * 1000))

        try:
            resp = await self._client.post(url, headers=self._get_headers(), json=payload)

            if resp.status_code != 200:
                logger.error(f"Grafana query failed: {resp.status_code} - {resp.text[:500]}")
                raise GrafanaQueryError(f"Query failed with status {resp.status_code}")

            grafana_result = resp.json()
            return self._convert_batch_grafana_response(grafana_result, queries.keys(), instant)

        except httpx.HTTPError as e:
            logger.error(f"Grafana request error: {e}")
            raise GrafanaQueryError(f"Request failed: {e}") from e

    def _convert_batch_grafana_response(
        self,
        grafana_result: dict[str, Any],
        ref_ids: Iterable[str],
        instant: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Convert a batched Grafana API response to Prometheus API format, keyed by refId.

        Args:
            grafana_result: The raw Grafana response JSON
            ref_ids: The refIds that were queried
            instant: Whether these were instant queries

        Returns:
            Dictionary mapping refId to list of metric results
        """
        all_results: dict[str, list[dict[str, Any]]] = {ref_id: [] for ref_id in ref_ids}
        ref_results = grafana_result.get("results", {})

        for ref_id, ref_result in ref_results.items():
            if ref_id not in all_results:
                continue
            frames = ref_result.get("frames", [])
            all_results[ref_id] = self._convert_frames(frames, instant)

        return all_results

    def _convert_grafana_response(self, grafana_result: dict[str, Any], instant: bool = True) -> list[dict[str, Any]]:
        """
        Convert Grafana API response to Prometheus API format.

        Grafana returns data in a different format than the Prometheus API.
        This method converts it to match the prometheus_api_client format.
        """
        batch = self._convert_batch_grafana_response(grafana_result, grafana_result.get("results", {}).keys(), instant)
        results: list[dict[str, Any]] = []
        for ref_results in batch.values():
            results.extend(ref_results)
        return results

    def _convert_frames(self, frames: list[dict[str, Any]], instant: bool) -> list[dict[str, Any]]:
        """Convert Grafana data frames to Prometheus API format."""
        results: list[dict[str, Any]] = []

        for frame in frames:
            schema = frame.get("schema", {})
            data = frame.get("data", {})

            labels: dict[str, str] = {}
            fields = schema.get("fields", [])
            for field in fields:
                field_labels = field.get("labels", {})
                if field_labels:
                    labels.update(field_labels)

            values = data.get("values", [])

            if instant:
                if values and len(values) > 1 and values[1]:
                    timestamp = values[0][0] if values[0] else 0
                    value = values[1][0] if values[1] else 0
                    results.append(
                        {
                            "metric": labels,
                            "value": [timestamp / 1000, str(value)],
                        }
                    )
            else:
                if values and len(values) > 1:
                    timestamps = values[0] or []
                    metric_values = values[1] or []

                    range_values = []
                    for i, ts in enumerate(timestamps):
                        if i < len(metric_values):
                            range_values.append([ts / 1000, str(metric_values[i])])

                    if range_values:
                        results.append(
                            {
                                "metric": labels,
                                "values": range_values,
                            }
                        )

        return results

    async def custom_query(self, query: str, datasource_uid: str | None = None) -> list[dict[str, Any]]:
        """
        Execute a custom PromQL query.

        Args:
            query: The PromQL query to execute.
            datasource_uid: Optional datasource UID override (e.g. for billing Mimir).

        Returns:
            List of metric results.
        """
        self._ensure_connected()
        logger.debug(f"Executing custom query: {query}")

        try:
            return await self._execute_query(query, instant=True, datasource_uid=datasource_uid)
        except Exception as e:
            logger.error(f"Failed to execute custom query: {e}")
            raise GrafanaQueryError(f"Failed to execute custom query: {e}") from e

    async def get_cpu_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
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
            return await self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query CPU usage: {e}")
            raise GrafanaQueryError(f"Failed to query CPU usage: {e}") from e

    async def get_memory_usage_by_namespace(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get memory usage metrics grouped by pod."""
        self._ensure_connected()

        if namespace:
            query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",container!=""}}) by (pod)'
        else:
            query = 'sum(container_memory_working_set_bytes{container!=""}) by (namespace, pod)'

        logger.debug(f"Querying memory usage: {query}")

        try:
            return await self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query memory usage: {e}")
            raise GrafanaQueryError(f"Failed to query memory usage: {e}") from e

    async def get_pod_count(self, namespace: str | None = None) -> int:
        """Get the count of running pods."""
        self._ensure_connected()

        query = f'count(kube_pod_info{{namespace="{namespace}"}})' if namespace else "count(kube_pod_info)"

        logger.debug(f"Querying pod count: {query}")

        try:
            result = await self._execute_query(query, instant=True)
            if result and len(result) > 0:
                return int(float(result[0]["value"][1]))
            return 0
        except Exception as e:
            logger.error(f"Failed to query pod count: {e}")
            raise GrafanaQueryError(f"Failed to query pod count: {e}") from e

    async def get_pod_restarts(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get pod restart counts."""
        self._ensure_connected()

        if namespace:
            query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}) by (pod)'
        else:
            query = "sum(kube_pod_container_status_restarts_total) by (namespace, pod)"

        logger.debug(f"Querying pod restarts: {query}")

        try:
            return await self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query pod restarts: {e}")
            raise GrafanaQueryError(f"Failed to query pod restarts: {e}") from e

    async def get_network_receive_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get network receive bytes rate."""
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_receive_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_receive_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network receive: {query}")

        try:
            return await self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query network receive: {e}")
            raise GrafanaQueryError(f"Failed to query network receive: {e}") from e

    async def get_network_transmit_bytes(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Get network transmit bytes rate."""
        self._ensure_connected()

        if namespace:
            query = f'sum(rate(container_network_transmit_bytes_total{{namespace="{namespace}"}}[5m])) by (pod)'
        else:
            query = "sum(rate(container_network_transmit_bytes_total[5m])) by (namespace, pod)"

        logger.debug(f"Querying network transmit: {query}")

        try:
            return await self._execute_query(query, instant=True)
        except Exception as e:
            logger.error(f"Failed to query network transmit: {e}")
            raise GrafanaQueryError(f"Failed to query network transmit: {e}") from e

    async def get_cluster_overview(self) -> dict[str, Any]:
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
            cpu_result = await self._execute_query(
                'sum(rate(container_cpu_usage_seconds_total{container!=""}[5m]))',
                instant=True,
            )
            if cpu_result:
                overview["total_cpu_usage"] = float(cpu_result[0]["value"][1])

            # Total memory usage
            memory_result = await self._execute_query(
                'sum(container_memory_working_set_bytes{container!=""})',
                instant=True,
            )
            if memory_result:
                overview["total_memory_bytes"] = int(float(memory_result[0]["value"][1]))

            # Pod count
            pod_result = await self._execute_query("count(kube_pod_info)", instant=True)
            if pod_result:
                overview["pod_count"] = int(float(pod_result[0]["value"][1]))

            # Namespace count
            ns_result = await self._execute_query(
                "count(count by (namespace) (kube_pod_info))",
                instant=True,
            )
            if ns_result:
                overview["namespace_count"] = int(float(ns_result[0]["value"][1]))

        except Exception as e:
            logger.error(f"Failed to get cluster overview: {e}")
            raise GrafanaQueryError(f"Failed to get cluster overview: {e}") from e

        return overview

    async def query_range(self, query: str, start_time: str, end_time: str, step: str) -> list[dict[str, Any]]:
        """Execute a range query to get time-series data."""
        self._ensure_connected()

        logger.debug(f"Executing range query: {query} from {start_time} to {end_time} step {step}")

        try:
            # Parse time strings to datetime
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)

            return await self._execute_query(
                query,
                instant=False,
                start_time=start_dt,
                end_time=end_dt,
                step=step,
            )
        except Exception as e:
            logger.error(f"Failed to execute range query: {e}")
            raise GrafanaQueryError(f"Failed to execute range query: {e}") from e

    async def get_component_metrics(
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
            # Batch CPU + memory into a single request
            batch_queries = {
                "cpu": (
                    f"avg(rate(container_cpu_usage_seconds_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                    f"}}[{time_range}]))"
                ),
                "memory": (
                    f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
                    f'pod=~"{pod_prefix}.*",container!=""}})'
                ),
            }

            batch_results = await self._execute_batch_queries(queries=batch_queries, instant=True)

            cpu_result = batch_results.get("cpu", [])
            if cpu_result:
                metrics["cpu_cores"] = float(cpu_result[0]["value"][1])

            memory_result = batch_results.get("memory", [])
            if memory_result:
                memory_bytes = float(memory_result[0]["value"][1])
                metrics["memory_bytes"] = memory_bytes
                metrics["memory_mb"] = memory_bytes / (1024 * 1024)

            # HTTP requests per second — try-until-found, stays sequential
            for req_metric in [
                "http_requests_total",
                "http_server_requests_seconds_count",
                "promhttp_metric_handler_requests_total",
            ]:
                req_query = f'sum(rate({req_metric}{{namespace="{namespace}",pod=~"{pod_prefix}.*"}}[{time_range}]))'
                try:
                    req_result = await self._execute_query(req_query, instant=True)
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

    async def get_deployment_component_metrics(
        self, namespace: str, components: list[str], deployment_name: str, time_range: str = "6h"
    ) -> dict[str, dict[str, float | None]]:
        """Get metrics for all components in a deployment."""
        pod_prefixes = {name: generate_unique_name(deployment_name, name) for name in components}

        tasks = {
            name: self.get_component_metrics(namespace, prefix, time_range) for name, prefix in pod_prefixes.items()
        }
        gathered = await asyncio.gather(*tasks.values())
        return dict(zip(tasks.keys(), gathered, strict=True))

    async def get_component_metrics_timeseries(
        self, namespace: str, pod_prefix: str, duration_minutes: int = 60, step_minutes: int = 5
    ) -> dict[str, Any]:
        """Get time-series metrics for a component over a duration."""
        if not GrafanaPrometheusConnector.is_connected:
            return {
                "cpu": [],
                "memory": [],
                "requests": [],
                "network_in": [],
                "network_out": [],
                "disk_read": [],
                "disk_write": [],
                "cpu_limit": None,
                "memory_limit": None,
                "cpu_timestamps": [],
                "memory_timestamps": [],
                "requests_timestamps": [],
                "network_timestamps": [],
                "disk_timestamps": [],
            }

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(minutes=duration_minutes)
        step = f"{step_minutes}m"

        result: dict[str, Any] = {
            "cpu": [],
            "memory": [],
            "requests": [],
            "network_in": [],
            "network_out": [],
            "disk_read": [],
            "disk_write": [],
            "cpu_limit": None,
            "memory_limit": None,
            "cpu_timestamps": [],
            "memory_timestamps": [],
            "requests_timestamps": [],
            "network_timestamps": [],
            "disk_timestamps": [],
        }

        try:
            # Batch all 7 range queries into a single Grafana request
            range_queries = {
                "cpu": (
                    f"sum(rate(container_cpu_usage_seconds_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*",container!=""'
                    f"}}[{step}]))"
                ),
                "memory": (
                    f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
                    f'pod=~"{pod_prefix}.*",container!=""}})'
                ),
                "requests": (
                    f'sum(rate(http_requests_total{{namespace="{namespace}",pod=~"{pod_prefix}.*"}}[{step}])) * 60'
                ),
                "network_in": (
                    f"sum(rate(container_network_receive_bytes_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                    f"}}[{step}])) / 1024"
                ),
                "network_out": (
                    f"sum(rate(container_network_transmit_bytes_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                    f"}}[{step}])) / 1024"
                ),
                "disk_read": (
                    f"sum(rate(container_fs_reads_bytes_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                    f"}}[{step}])) / 1024"
                ),
                "disk_write": (
                    f"sum(rate(container_fs_writes_bytes_total{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*"'
                    f"}}[{step}])) / 1024"
                ),
            }

            range_results = await self._execute_batch_queries(
                queries=range_queries,
                instant=False,
                start_time=start_time,
                end_time=end_time,
                step=step,
            )

            # Process range results
            converters: dict[str, tuple[str, float, int]] = {
                # refId -> (result_key, multiplier, rounding)
                "cpu": ("cpu", 1000, 2),  # cores -> millicores
                "memory": ("memory", 1 / (1024 * 1024), 1),  # bytes -> MB
                "requests": ("requests", 1, 1),
                "network_in": ("network_in", 1, 2),
                "network_out": ("network_out", 1, 2),
                "disk_read": ("disk_read", 1, 2),
                "disk_write": ("disk_write", 1, 2),
            }

            for ref_id, (key, multiplier, rounding) in converters.items():
                data = range_results.get(ref_id, [])
                if data and "values" in data[0]:
                    for ts, value in data[0]["values"]:
                        result[key].append({"timestamp": ts, "value": round(float(value) * multiplier, rounding)})

            # Extract timestamps
            result["cpu_timestamps"] = [item["timestamp"] for item in result["cpu"]]
            result["memory_timestamps"] = [item["timestamp"] for item in result["memory"]]
            result["requests_timestamps"] = [item["timestamp"] for item in result["requests"]]
            result["network_timestamps"] = [item["timestamp"] for item in result["network_in"]]
            result["disk_timestamps"] = [item["timestamp"] for item in result["disk_read"]]

            # Batch both instant limit queries into a single request
            limit_queries = {
                "cpu_limit": (
                    f"sum(kube_pod_container_resource_limits{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="cpu"'
                    f"}})"
                ),
                "memory_limit": (
                    f"sum(kube_pod_container_resource_limits{{"
                    f'namespace="{namespace}",pod=~"{pod_prefix}.*",resource="memory"'
                    f"}})"
                ),
            }

            limit_results = await self._execute_batch_queries(queries=limit_queries, instant=True)

            cpu_limit_data = limit_results.get("cpu_limit", [])
            if cpu_limit_data:
                cpu_limit_cores = float(cpu_limit_data[0]["value"][1])
                result["cpu_limit"] = round(cpu_limit_cores * 1000, 0)

            memory_limit_data = limit_results.get("memory_limit", [])
            if memory_limit_data:
                memory_limit_bytes = float(memory_limit_data[0]["value"][1])
                result["memory_limit"] = round(memory_limit_bytes / (1024 * 1024), 0)

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
        """Get time-series metrics for all components in a deployment."""
        pod_prefixes = {name: generate_unique_name(deployment_name, name) for name in components}

        tasks = {
            name: self.get_component_metrics_timeseries(namespace, prefix, duration_minutes, step_minutes)
            for name, prefix in pod_prefixes.items()
        }
        gathered = await asyncio.gather(*tasks.values())
        return dict(zip(tasks.keys(), gathered, strict=True))

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
        if not GrafanaPrometheusConnector.is_connected:
            return {}

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(minutes=duration_minutes)
        step = f"{step_minutes}m"

        result: dict[str, dict[str, Any]] = {}

        try:
            # Query all PVC usage in the namespace - simple namespace filter only
            pvc_used_query = f'kubelet_volume_stats_used_bytes{{namespace="{namespace}"}}'
            pvc_used_result = await self._execute_query(
                pvc_used_query, instant=False, start_time=start_time, end_time=end_time, step=step
            )

            # Get PVC capacities (current values)
            pvc_capacity_query = f'kubelet_volume_stats_capacity_bytes{{namespace="{namespace}"}}'
            pvc_capacity_result = await self._execute_query(pvc_capacity_query, instant=True)

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
        """Discover workloads in a namespace via Prometheus metrics."""
        if not GrafanaPrometheusConnector.is_connected:
            logger.warning("Grafana not connected, cannot discover workloads")
            return []

        workloads: dict[str, dict[str, Any]] = {}

        try:
            query = f'kube_pod_info{{namespace="{namespace}"}}'
            logger.debug(f"Discovering workloads with query: {query}")

            result = await self._execute_query(query, instant=True)

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
                        if (
                            len(parts) >= 2
                            and len(parts[-1]) >= 5
                            and parts[-1].isalnum()
                            and not (parts[-1].isdigit() or (len(parts) >= 3 and len(parts[-2]) >= 5))
                        ):
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

            if len(last_part) == 5 and last_part.isalnum() and len(second_last) >= 5 and second_last.isalnum():
                return "-".join(parts[:-2])

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
        """Get time-series metrics for discovered workloads."""
        names = [w.get("name", "") for w in workloads if w.get("name")]

        tasks = {
            name: self.get_component_metrics_timeseries(namespace, name, duration_minutes, step_minutes)
            for name in names
        }
        gathered = await asyncio.gather(*tasks.values())
        return dict(zip(tasks.keys(), gathered, strict=True))


async def create_grafana_prometheus_connector() -> GrafanaPrometheusConnector:
    """Create and return a GrafanaPrometheusConnector instance."""
    logger.debug("Creating GrafanaPrometheusConnector")
    connector = GrafanaPrometheusConnector()
    await connector.connect()
    return connector
