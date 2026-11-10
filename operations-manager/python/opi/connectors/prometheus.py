"""Query Prometheus directly, without Grafana in between."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import requests
from prometheus_api_client.exceptions import PrometheusApiClientException

# Import from the submodule, not the package root: the root's module-level __getattr__ shim
# makes a type checker resolve PrometheusConnect to a union and reject valid arguments.
from prometheus_api_client.prometheus_connect import PrometheusConnect

from opi.connectors.grafana_prometheus import close_grafana_connector
from opi.connectors.metrics_backend import MetricsQueryError, parse_query_time
from opi.core.config import settings

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

#: Every call to Prometheus, PromQL or series API, gives up after this long.
#: Without it the library waits indefinitely while the loop holds a thread.
HTTP_TIMEOUT_SECONDS = 10


class PrometheusConnector:
    _instance: PrometheusConnector | None = None
    is_connected: bool = False
    prom: Any  # PrometheusConnect instance (untyped library)
    _initialized: bool
    _prometheus_url: str

    def __new__(cls) -> PrometheusConnector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Build the HTTP client. There is no connection to verify: every query is its own request."""
        if self._initialized:
            return

        self._initialized = True
        self._prometheus_url = getattr(settings, "PROMETHEUS_URL", "http://prometheus.rig-system:9090")
        logger.info(f"Initializing PrometheusConnector with URL: {self._prometheus_url}")

        self.prom = PrometheusConnect(url=self._prometheus_url, timeout=HTTP_TIMEOUT_SECONDS)
        PrometheusConnector.is_connected = True

    def start_probe(self) -> None:
        """No-op: there is nothing to probe. Keeps the shape of the Grafana connector,
        which get_metrics_connector() returns in its place."""

    async def query(
        self, expr: str, *, at: datetime | None = None, datasource_uid: str | None = None
    ) -> list[dict[str, Any]]:
        """Run an instant query, by default evaluated now.

        In a thread: prometheus_api_client is synchronous and talks through ``requests``, so a
        direct call holds the event loop for the whole request, DNS and retries included.

        Raises:
            ValueError: when a datasource_uid is given. This connector has one store;
                picking a datasource exists only on the Grafana backend.
            MetricsQueryError: when the query fails.
        """
        if datasource_uid is not None:
            raise ValueError("datasource_uid is only supported on the Grafana metrics backend")

        logger.debug(f"Executing instant query: {expr}")
        params = {"time": at.timestamp()} if at is not None else None
        try:
            result: list[dict[str, Any]] = await asyncio.to_thread(self.prom.custom_query, expr, params=params)
            return result
        except Exception as e:
            logger.error(f"Failed to execute instant query: {e}")
            raise MetricsQueryError(f"Failed to execute instant query: {e}") from e

    async def query_batch(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        """Run instant queries and return the results per key.

        One HTTP request per query, sequentially inside one thread: the library is synchronous,
        so the thread is what keeps the event loop free.
        """
        return await asyncio.to_thread(self._run_instant, queries)

    def _run_instant(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        logger.debug(f"Executing {len(queries)} instant quer(y|ies)")
        try:
            return {key: self.prom.custom_query(query) for key, query in queries.items()}
        except (requests.RequestException, PrometheusApiClientException) as e:
            logger.error(f"Failed to execute instant queries: {e}")
            raise MetricsQueryError(f"Failed to execute instant queries: {e}") from e

    async def query_range_batch(
        self, queries: dict[str, str], start_time: datetime, end_time: datetime, step: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Run range queries over one window and return the results per key."""
        return await asyncio.to_thread(self._run_range, queries, start_time, end_time, step)

    def _run_range(
        self, queries: dict[str, str], start_time: datetime, end_time: datetime, step: str
    ) -> dict[str, list[dict[str, Any]]]:
        logger.debug(f"Executing {len(queries)} range quer(y|ies) from {start_time} to {end_time} step {step}")
        try:
            return {
                key: self.prom.custom_query_range(query=query, start_time=start_time, end_time=end_time, step=step)
                for key, query in queries.items()
            }
        except (requests.RequestException, PrometheusApiClientException) as e:
            logger.error(f"Failed to execute range queries: {e}")
            raise MetricsQueryError(f"Failed to execute range queries: {e}") from e

    def discover_metric_names(self, match_selector: str) -> list[str]:
        """The metric names matching a label matcher such as '{job="minio"}', sorted.

        Through ``/api/v1/series``, which searches the TSDB index: a job with a scrape interval
        longer than the 5-minute staleness window returns nothing on a PromQL instant query.
        """
        url = f"{self._prometheus_url.rstrip('/')}/api/v1/series"
        logger.debug(f"Discovering metric names via series API: match[]={match_selector}")

        try:
            response = requests.get(url, params={"match[]": match_selector}, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise MetricsQueryError(f"Series API returned status: {data.get('status')}")

            names: set[str] = set()
            for series in data.get("data", []):
                name = series.get("__name__")
                if name:
                    names.add(name)

            return sorted(names)
        except requests.RequestException as e:
            logger.error(f"Failed to discover metric names for {match_selector}: {e}")
            raise MetricsQueryError(f"Failed to discover metric names: {e}") from e

    async def query_range(
        self, query: str, start_time: str | int | float, end_time: str | int | float, step: str
    ) -> list[dict[str, Any]]:
        """Run a range query; the bounds take RFC3339 or a Unix timestamp, the step e.g. "5m".

        In a thread for the same reason as :meth:`query`. prometheus_api_client wants datetime
        bounds; a string here failed every range query on the sandbox.
        """
        logger.debug(f"Executing range query: {query} from {start_time} to {end_time} step {step}")

        try:
            result: list[dict[str, Any]] = await asyncio.to_thread(
                self.prom.custom_query_range,
                query=query,
                start_time=parse_query_time(start_time),
                end_time=parse_query_time(end_time),
                step=step,
            )
            return result
        except Exception as e:
            logger.error(f"Failed to execute range query: {e}")
            raise MetricsQueryError(f"Failed to execute range query: {e}") from e


def create_prometheus_connector() -> PrometheusConnector:
    """The PrometheusConnector singleton."""
    logger.debug("Creating PrometheusConnector")
    return PrometheusConnector()


async def get_metrics_connector() -> Any:
    """The connector singleton for the configured ``METRICS_BACKEND``.

    PrometheusConnector queries Prometheus directly (sandbox), GrafanaPrometheusConnector goes
    through Grafana (ODCN production and staging). Both satisfy
    ``connectors.metrics_backend.MetricsBackend``; the queries live in
    ``services.metrics_queries``.
    """
    from opi.core.config import settings

    if settings.METRICS_BACKEND == "grafana":
        from opi.connectors.grafana_prometheus import create_grafana_prometheus_connector

        logger.info("Using Grafana-based metrics connector (METRICS_BACKEND=grafana)")
        return await create_grafana_prometheus_connector()
    else:
        logger.info("Using direct Prometheus connector (METRICS_BACKEND=prometheus)")
        return create_prometheus_connector()


async def close_metrics_connector() -> None:
    """Release the metrics connector's resources at shutdown."""
    await close_grafana_connector()
