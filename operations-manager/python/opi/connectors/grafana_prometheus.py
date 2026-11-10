"""Query Prometheus/Mimir through Grafana's datasource API, with a service account token.

The route for ODCN, where Prometheus itself is not reachable.
"""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from opi.connectors.metrics_backend import MetricsQueryError, MetricsUnavailableError, parse_query_time
from opi.core.config import settings
from opi.utils.connect_retry import backoff_delays

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

logger = logging.getLogger(__name__)

#: Waits between recovery probes, doubling from the first up to the ceiling and then
#: repeating that until the backend answers.
RECOVERY_FIRST_DELAY = 5.0
RECOVERY_MAX_DELAY = 60.0


class GrafanaPrometheusConnector:
    """The same interface as PrometheusConnector, over Grafana's ``/api/ds/query``."""

    _instance: GrafanaPrometheusConnector | None = None
    is_connected: bool = False
    _initialized: bool
    _grafana_url: str
    _token: str | None
    _datasource_uid: str | None
    _datasource_type: str
    _client: httpx.AsyncClient
    _recovery_task: asyncio.Task[None] | None
    _recovery_first_delay: float
    _recovery_max_delay: float
    _unavailable_since: float | None

    def __new__(cls) -> GrafanaPrometheusConnector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Config only, no I/O: ``start_probe`` makes the first call."""
        if self._initialized:
            return

        self._initialized = True
        self._grafana_url = settings.GRAFANA_URL.rstrip("/")
        self._token = settings.GRAFANA_TOKEN
        self._datasource_uid = settings.GRAFANA_DATASOURCE_UID
        self._datasource_type = "prometheus"
        # One client for the whole process. A client per attempt leaked a connection
        # pool per request for as long as Grafana was unreachable.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))
        self._recovery_task = None
        self._recovery_first_delay = RECOVERY_FIRST_DELAY
        self._recovery_max_delay = RECOVERY_MAX_DELAY
        self._unavailable_since = None
        # Unavailable until a probe says otherwise, so a page right after the boot says
        # "not available" instead of claiming metrics it never fetched.
        GrafanaPrometheusConnector.is_connected = False

        logger.debug(f"Initializing GrafanaPrometheusConnector with URL: {self._grafana_url}")

        if not self._token:
            logger.warning("GRAFANA_TOKEN not set - Grafana connector will not work")

    def start_probe(self) -> None:
        """Probe the backend in the background. Called once at startup."""
        self._start_recovery(immediate=True)

    async def aclose(self) -> None:
        """Stop the recovery task and close the HTTP client."""
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        await self._client.aclose()

    def _mark_available(self) -> None:
        """Record that the backend answered. Logs the transition only."""
        if GrafanaPrometheusConnector.is_connected:
            return

        GrafanaPrometheusConnector.is_connected = True
        if self._unavailable_since is None:
            logger.info("Grafana metrics are available")
        else:
            logger.info("Grafana metrics are available again after %.0fs", time.monotonic() - self._unavailable_since)
        self._unavailable_since = None

    def _mark_unavailable(self, reason: str) -> None:
        """Record a backend failure and keep one recovery task running until it answers.

        The reason holds a status code or an exception type, never headers or the
        request: the service account token must stay out of the logs.
        """
        if GrafanaPrometheusConnector.is_connected or self._unavailable_since is None:
            logger.warning("Grafana metrics are unavailable: %s", reason)
            self._unavailable_since = time.monotonic()
        else:
            logger.debug("Grafana metrics still unavailable: %s", reason)

        GrafanaPrometheusConnector.is_connected = False
        self._start_recovery()

    def _start_recovery(self, *, immediate: bool = False) -> None:
        """Start the recovery task unless one is already running."""
        if not self._token:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(self._recover(immediate=immediate))

    async def _recover(self, *, immediate: bool) -> None:
        """Probe until the backend answers, then stop.

        No attempt limit: giving up would leave the pod without metrics until someone
        restarts it.
        """
        if immediate and await self._probe():
            return

        for delay in backoff_delays(self._recovery_first_delay, self._recovery_max_delay):
            await asyncio.sleep(delay)
            # A query may have restored the status while this task was waiting.
            if GrafanaPrometheusConnector.is_connected or await self._probe():
                return

    async def _probe(self) -> bool:
        """One availability check: find the datasource if needed, then query ``up``."""
        try:
            if not self._datasource_uid:
                await self._discover_datasource()
            if not self._datasource_uid:
                self._mark_unavailable("no prometheus or mimir datasource in Grafana")
                return False
            await self._execute_query("up", instant=True)
        except Exception as e:
            # Broad on purpose, against the house rule: anything escaping here kills the
            # recovery task, and a dead recovery task is the failure this replaces.
            logger.debug("Grafana probe failed: %s", e)
            return False
        return True

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None, *, mark_available: bool = True
    ) -> httpx.Response:
        """One call to Grafana, with the availability bookkeeping around it.

        A transport error, a 5xx or a 401/403 says the backend is unreachable or the
        token is wrong: that flips the status and starts the recovery task. Only a 2xx
        marks it available; any other status is the query's own problem and leaves the
        status alone. Datasource discovery passes ``mark_available=False``: a datasource
        list says nothing about whether metrics can be queried.
        """
        try:
            resp = await self._client.request(
                method, f"{self._grafana_url}{path}", headers=self._get_headers(), json=json_body
            )
        except httpx.TransportError as e:
            self._mark_unavailable(f"{type(e).__name__}: {e}")
            raise MetricsUnavailableError(f"Grafana is unreachable: {type(e).__name__}") from e

        if resp.status_code >= 500 or resp.status_code in (401, 403):
            self._mark_unavailable(f"HTTP {resp.status_code}")
            raise MetricsUnavailableError(f"Grafana answered HTTP {resp.status_code}")

        if resp.is_success and mark_available:
            self._mark_available()
        return resp

    async def _discover_datasource(self) -> None:
        logger.debug("Auto-discovering Prometheus/Mimir datasource...")

        # Failures go to debug: the probe reports them through _mark_unavailable, which
        # logs the transition only.
        resp = await self._request("GET", "/api/datasources", mark_available=False)
        if resp.status_code != 200:
            logger.debug(f"Failed to fetch datasources: {resp.status_code}")
            return

        for ds in resp.json():
            ds_type = ds.get("type", "")
            if ds_type in ("prometheus", "mimir"):
                self._datasource_uid = ds.get("uid")
                self._datasource_type = ds_type
                logger.info(
                    f"Auto-discovered datasource: {ds.get('name')} (type: {ds_type}, uid: {self._datasource_uid})"
                )
                return

        logger.debug("No Prometheus/Mimir datasource found in Grafana")

    def _ensure_connected(self) -> None:
        """Refuse a query while the backend is known to be unavailable.

        Without this every caller would wait out the client timeouts again during an
        outage, with a page or a job behind it.
        """
        if not GrafanaPrometheusConnector.is_connected:
            raise MetricsUnavailableError("Grafana metrics are not available")

    def _build_query_obj(
        self, ref_id: str, query: str, instant: bool, step: str = "5m", datasource_uid: str | None = None
    ) -> dict[str, Any]:
        """Build a single query object for the Grafana API.

        ``step`` goes out as Grafana's ``interval``, which Grafana treats as a MINIMUM: it
        derives the real resolution from the time range and maxDataPoints. Over a long
        range the answer therefore has fewer points than the same step through Prometheus.
        """
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
        """One query, in Prometheus API format; ``datasource_uid`` overrides the configured one."""
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
        """Several queries in one Grafana request, keyed by refId.

        All queries in a batch share the same instant/range mode and time window.
        """
        if not queries:
            return {}

        query_objects = [
            self._build_query_obj(ref_id, query, instant, step, datasource_uid) for ref_id, query in queries.items()
        ]

        payload: dict[str, Any] = {
            "queries": query_objects,
            "from": "now-5m",
            "to": "now",
        }

        if start_time and end_time:
            payload["from"] = str(int(start_time.timestamp() * 1000))
            payload["to"] = str(int(end_time.timestamp() * 1000))
        elif end_time:
            payload["from"] = str(int(end_time.timestamp() * 1000) - 60_000)
            payload["to"] = str(int(end_time.timestamp() * 1000))

        resp = await self._request("POST", "/api/ds/query", payload)

        if resp.status_code != 200:
            logger.error(f"Grafana query failed: {resp.status_code} - {resp.text[:500]}")
            raise MetricsQueryError(f"Query failed with status {resp.status_code}")

        grafana_result = resp.json()
        self._log_response_problems(grafana_result, queries)
        return self._convert_batch_grafana_response(grafana_result, queries.keys(), instant)

    def _log_response_problems(self, grafana_result: dict[str, Any], queries: dict[str, str]) -> None:
        """Report problems that Grafana returns inside a 200 OK body.

        /api/ds/query answers 200 even when the datasource itself fails, and puts the reason in
        results[refId].error. Without this the caller sees an empty result and nothing is logged.
        """
        ref_results = grafana_result.get("results", {})

        if not ref_results:
            logger.error(f"Grafana returned 200 without any results for {len(queries)} quer(y|ies)")
            return

        empty: list[str] = []
        for ref_id, ref_result in ref_results.items():
            error = ref_result.get("error")
            if error:
                source = ref_result.get("errorSource", "unknown")
                status = ref_result.get("status", "?")
                logger.error(
                    f"Grafana query {ref_id} failed (status={status}, source={source}): {str(error)[:300]} "
                    f":: {queries.get(ref_id, '?')[:200]}"
                )
            elif not self._count_datapoints(ref_result.get("frames", [])):
                # A frame with an empty value column still counts as no data: that is what
                # a datasource returns when the series stopped being ingested.
                empty.append(ref_id)

        if empty and len(empty) == len(ref_results):
            logger.warning(
                f"Grafana returned no data for all {len(empty)} quer(y|ies); "
                f"first query was: {queries.get(empty[0], '?')[:200]}"
            )

    @staticmethod
    def _count_datapoints(frames: list[dict[str, Any]]) -> int:
        """Count the actual values in a set of frames, ignoring empty value columns."""
        total = 0
        for frame in frames:
            values = frame.get("data", {}).get("values", [])
            if len(values) > 1 and values[1]:
                total += len(values[1])
        return total

    def _convert_batch_grafana_response(
        self,
        grafana_result: dict[str, Any],
        ref_ids: Iterable[str],
        instant: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Convert a batched Grafana response to Prometheus API format, keyed by refId."""
        all_results: dict[str, list[dict[str, Any]]] = {ref_id: [] for ref_id in ref_ids}
        ref_results = grafana_result.get("results", {})

        for ref_id, ref_result in ref_results.items():
            if ref_id not in all_results:
                continue
            frames = ref_result.get("frames", [])
            all_results[ref_id] = self._convert_frames(frames, instant)

        return all_results

    def _convert_frames(self, frames: list[dict[str, Any]], instant: bool) -> list[dict[str, Any]]:
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

    async def query(
        self, expr: str, *, at: datetime | None = None, datasource_uid: str | None = None
    ) -> list[dict[str, Any]]:
        """Run an instant query, by default evaluated at the end of a five-minute window.

        Grafana evaluates the minute before ``at``. ``datasource_uid`` picks another datasource
        than the configured one, for billing Mimir.

        Raises:
            MetricsUnavailableError: when the backend is known to be unavailable.
            MetricsQueryError: when the query fails.
        """
        self._ensure_connected()
        logger.debug(f"Executing instant query: {expr}")

        try:
            return await self._execute_query(expr, instant=True, datasource_uid=datasource_uid, end_time=at)
        except MetricsUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Failed to execute instant query: {e}")
            raise MetricsQueryError(f"Failed to execute instant query: {e}") from e

    async def query_batch(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        """Run instant queries in a single Grafana request, results per key."""
        self._ensure_connected()
        return await self._execute_batch_queries(queries=queries, instant=True)

    async def query_range_batch(
        self, queries: dict[str, str], start_time: datetime, end_time: datetime, step: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Run range queries over one window in a single Grafana request, results per key."""
        self._ensure_connected()
        return await self._execute_batch_queries(
            queries=queries, instant=False, start_time=start_time, end_time=end_time, step=step
        )

    async def query_range(
        self, query: str, start_time: str | int | float, end_time: str | int | float, step: str
    ) -> list[dict[str, Any]]:
        """Run a range query; the bounds take Unix-epoch seconds or an ISO-8601 string."""
        self._ensure_connected()

        logger.debug(f"Executing range query: {query} from {start_time} to {end_time} step {step}")

        try:
            start_dt = parse_query_time(start_time)
            end_dt = parse_query_time(end_time)

            return await self._execute_query(
                query,
                instant=False,
                start_time=start_dt,
                end_time=end_dt,
                step=step,
            )
        except MetricsUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Failed to execute range query: {e}")
            raise MetricsQueryError(f"Failed to execute range query: {e}") from e


async def create_grafana_prometheus_connector() -> GrafanaPrometheusConnector:
    """Return the GrafanaPrometheusConnector singleton. Does no I/O."""
    logger.debug("Creating GrafanaPrometheusConnector")
    return GrafanaPrometheusConnector()


async def close_grafana_connector() -> None:
    """Release the connector's resources at shutdown, if it was ever built."""
    if GrafanaPrometheusConnector._instance is not None:
        await GrafanaPrometheusConnector._instance.aclose()
