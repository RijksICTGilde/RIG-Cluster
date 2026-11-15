"""The two metrics backends, fed the same answer, measured side by side.

``get_metrics_connector()`` hands out ``PrometheusConnector`` on the sandbox and
``GrafanaPrometheusConnector`` in production, and a caller is not supposed to notice which
one it got. These tests pin what the two do with identical data, including the places
where they still disagree: the limit aggregation (``max`` against ``sum``), the step on a
range query, and which exception a failure becomes.

A difference asserted here is a difference that exists today, not one that is wanted.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import requests
from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector
from opi.connectors.metrics_backend import MetricsQueryError, MetricsUnavailableError
from opi.connectors.prometheus import HTTP_TIMEOUT_SECONDS, PrometheusConnector
from opi.services.metrics_queries import TimeWindow, component_timeseries
from prometheus_api_client.exceptions import PrometheusApiClientException

#: Two pods, each with the same limit and request, so an aggregation over the pods is
#: visible in the outcome: ``max`` answers one pod, ``sum`` answers both.
POD_COUNT = 2
CPU_LIMIT_CORES = 0.5
MEMORY_LIMIT_BYTES = 536870912.0
MEMORY_REQUEST_BYTES = 268435456.0

#: Range answers keyed by the metric the query asks for, in seconds and base units.
RANGE_SERIES: dict[str, list[tuple[int, float]]] = {
    "container_cpu_usage_seconds_total": [(1000, 0.05), (2000, 0.10)],
    "container_memory_working_set_bytes": [(1000, 104857600.0), (2000, 209715200.0)],
    "container_network_receive_bytes_total": [(1000, 2048.0), (2000, 4096.0)],
    "container_network_transmit_bytes_total": [(1000, 1024.0), (2000, 2048.0)],
    "container_fs_reads_bytes_total": [(1000, 512.0), (2000, 1024.0)],
    "container_fs_writes_bytes_total": [(1000, 256.0), (2000, 512.0)],
}

#: Instant answers per pod, keyed by metric and the ``resource`` label.
INSTANT_PER_POD: dict[tuple[str, str], float] = {
    ("kube_pod_container_resource_limits", "cpu"): CPU_LIMIT_CORES,
    ("kube_pod_container_resource_limits", "memory"): MEMORY_LIMIT_BYTES,
    ("kube_pod_container_resource_requests", "memory"): MEMORY_REQUEST_BYTES,
}


def range_answer(query: str) -> list[tuple[int, float]]:
    for metric, values in RANGE_SERIES.items():
        if metric in query:
            return values
    return []


def instant_answer(query: str) -> float | None:
    """What a two-pod namespace answers, with the query's own aggregation applied."""
    resource = re.search(r'resource="(\w+)"', query)
    for (metric, label), per_pod in INSTANT_PER_POD.items():
        if metric in query and resource is not None and resource.group(1) == label:
            return per_pod * POD_COUNT if query.startswith("sum(") else per_pod
    return None


@pytest.fixture
def prometheus() -> PrometheusConnector:
    """A connected PrometheusConnector whose library client answers from the tables above."""
    connector = PrometheusConnector.__new__(PrometheusConnector)
    connector._initialized = True
    connector._prometheus_url = "http://prometheus:9090"
    connector.prom = MagicMock()
    connector.prom.custom_query_range.side_effect = lambda query, **_: [
        {"metric": {}, "values": [[ts, str(value)] for ts, value in range_answer(query)]}
    ]
    connector.prom.custom_query.side_effect = lambda query, **_: (
        [] if instant_answer(query) is None else [{"metric": {}, "value": [1000, str(instant_answer(query))]}]
    )
    PrometheusConnector.is_connected = True
    PrometheusConnector._instance = connector
    yield connector
    PrometheusConnector._instance = None


def grafana_frames(request: httpx.Request) -> httpx.Response:
    """Answer a /api/ds/query payload from the same tables, in Grafana's frame format."""
    payload = json.loads(request.content)
    results: dict[str, Any] = {}
    for query_obj in payload["queries"]:
        expr = query_obj["expr"]
        if query_obj["instant"]:
            value = instant_answer(expr)
            values = [[1000], [value]] if value is not None else [[], []]
        else:
            series = range_answer(expr)
            values = [[ts * 1000 for ts, _ in series], [value for _, value in series]]
        results[query_obj["refId"]] = {
            "frames": [{"schema": {"fields": [{}, {"labels": {}}]}, "data": {"values": values}}]
        }
    return httpx.Response(200, json={"results": results})


@pytest.fixture
def grafana(monkeypatch: pytest.MonkeyPatch) -> GrafanaPrometheusConnector:
    """A connected GrafanaPrometheusConnector answering from the same tables."""
    GrafanaPrometheusConnector._instance = None
    monkeypatch.setattr("opi.core.config.settings.GRAFANA_TOKEN", "test-token")
    monkeypatch.setattr("opi.core.config.settings.GRAFANA_DATASOURCE_UID", "test-uid")
    connector = GrafanaPrometheusConnector()
    connector._recovery_delays = (3600.0,)
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(grafana_frames))
    GrafanaPrometheusConnector.is_connected = True
    yield connector
    if connector._recovery_task is not None:
        connector._recovery_task.cancel()
    GrafanaPrometheusConnector._instance = None
    GrafanaPrometheusConnector.is_connected = False


async def timeseries(connector: Any) -> dict[str, Any]:
    return await component_timeseries(connector, "rig-demo", "production-api", TimeWindow())


class TestComponentTimeseries:
    """The same data through both backends."""

    @pytest.mark.asyncio
    async def test_series_and_units_are_identical(
        self, prometheus: PrometheusConnector, grafana: GrafanaPrometheusConnector
    ) -> None:
        """CPU in millicores, memory in MB, network and disk in KB/s: the same numbers."""
        direct = await timeseries(prometheus)
        via_grafana = await timeseries(grafana)

        for key in ("cpu", "memory", "network_in", "network_out", "disk_read", "disk_write"):
            assert direct[key] == via_grafana[key], key

        assert direct["cpu"] == [{"timestamp": 1000, "value": 50.0}, {"timestamp": 2000, "value": 100.0}]
        assert direct["memory"] == [{"timestamp": 1000, "value": 100.0}, {"timestamp": 2000, "value": 200.0}]
        assert direct["cpu_timestamps"] == via_grafana["cpu_timestamps"] == [1000, 2000]

    @pytest.mark.asyncio
    async def test_limits_differ_because_of_the_aggregation(
        self, prometheus: PrometheusConnector, grafana: GrafanaPrometheusConnector
    ) -> None:
        """Prometheus aggregates the limit with ``max``, Grafana with ``sum``.

        With two pods the Grafana graph therefore draws the limit twice as high. Pinned,
        not endorsed: making both ``max`` is a card of its own.
        """
        direct = await timeseries(prometheus)
        via_grafana = await timeseries(grafana)

        assert direct["cpu_limit"] == 500.0
        assert direct["memory_limit"] == 512.0
        assert direct["memory_request"] == 256.0

        assert via_grafana["cpu_limit"] == 500.0 * POD_COUNT
        assert via_grafana["memory_limit"] == 512.0 * POD_COUNT
        assert via_grafana["memory_request"] == 256.0 * POD_COUNT

    @pytest.mark.asyncio
    async def test_prometheus_asks_max_and_grafana_asks_sum(
        self, prometheus: PrometheusConnector, grafana: GrafanaPrometheusConnector
    ) -> None:
        """The aggregation lives in the PromQL, which is what the two disagree on."""
        await timeseries(prometheus)
        limit_queries = [
            call.args[0]
            for call in prometheus.prom.custom_query.call_args_list
            if "kube_pod_container_resource" in call.args[0]
        ]
        assert len(limit_queries) == 3
        assert all(query.startswith("max(") for query in limit_queries)

        sent: list[dict[str, Any]] = []
        grafana._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _record(request, sent, grafana_frames))
        )
        await timeseries(grafana)
        grafana_limits = [
            q["expr"] for payload in sent for q in payload["queries"] if "kube_pod_container_resource" in q["expr"]
        ]
        assert len(grafana_limits) == 3
        assert all(expr.startswith("sum(") for expr in grafana_limits)


def _record(request: httpx.Request, sent: list[dict[str, Any]], handler: Any) -> httpx.Response:
    sent.append(json.loads(request.content))
    return handler(request)


class TestRangeQueryStep:
    """What each backend does with the ``step`` a caller asks for."""

    @pytest.mark.asyncio
    async def test_prometheus_passes_the_step_through(self, prometheus: PrometheusConnector) -> None:
        await prometheus.query_range("up", start_time="1781530694", end_time="1781532494", step="300")

        kwargs = prometheus.prom.custom_query_range.call_args.kwargs
        assert kwargs["step"] == "300"
        assert kwargs["start_time"] == datetime.fromtimestamp(1781530694, tz=UTC)
        assert kwargs["end_time"] == datetime.fromtimestamp(1781532494, tz=UTC)

    @pytest.mark.parametrize("step", ["300", "5m"])
    @pytest.mark.parametrize("span_seconds", [3600, 7 * 24 * 3600])
    @pytest.mark.asyncio
    async def test_grafana_sends_the_step_as_a_minimum_interval(
        self, grafana: GrafanaPrometheusConnector, step: str, span_seconds: int
    ) -> None:
        """Grafana gets ``interval``, which it treats as a floor, not as the resolution.

        It derives the real step from the time range and ``maxDataPoints``, so over a long
        range the sandbox and production draw a different number of points. Pinned here:
        no ``intervalMs`` and no ``maxDataPoints`` go out, so nothing fixes the step.
        """
        sent: list[dict[str, Any]] = []
        grafana._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _record(request, sent, grafana_frames))
        )
        end = 1781532494
        start = end - span_seconds

        await grafana.query_range("up", start_time=str(start), end_time=str(end), step=step)

        payload = sent[0]
        assert payload["from"] == str(start * 1000)
        assert payload["to"] == str(end * 1000)
        query_obj = payload["queries"][0]
        assert query_obj["interval"] == step
        assert query_obj["range"] is True
        assert "intervalMs" not in query_obj
        assert "maxDataPoints" not in payload


class TestGrafanaFrameValues:
    """A frame carries JSON values; a missing or non-numeric one still becomes a string."""

    def test_null_and_nan_become_strings(self, grafana: GrafanaPrometheusConnector) -> None:
        frames = [
            {
                "schema": {"fields": [{}, {"labels": {"pod": "api-0"}}]},
                "data": {"values": [[1000, 2000, 3000], [1.5, None, float("nan")]]},
            }
        ]

        instant = grafana._convert_frames(frames, instant=True)
        assert instant == [{"metric": {"pod": "api-0"}, "value": [1.0, "1.5"]}]

        series = grafana._convert_frames(frames, instant=False)
        assert series[0]["values"] == [[1.0, "1.5"], [2.0, "None"], [3.0, "nan"]]

    def test_an_empty_value_column_yields_no_series(self, grafana: GrafanaPrometheusConnector) -> None:
        frames = [{"schema": {"fields": [{}, {"labels": {}}]}, "data": {"values": [[], []]}}]

        assert grafana._convert_frames(frames, instant=True) == []
        assert grafana._convert_frames(frames, instant=False) == []


class TestFailureTranslation:
    """Which exception a failure becomes, and whether availability flips with it."""

    @pytest.mark.asyncio
    async def test_prometheus_turns_every_failure_into_a_query_error(self, prometheus: PrometheusConnector) -> None:
        """Unreachable and a 500 are the same exception here, and never flip availability."""
        for failure in (
            requests.ConnectionError("name or service not known"),
            PrometheusApiClientException("HTTP Status Code 500"),
        ):
            prometheus.prom.custom_query.side_effect = failure
            with pytest.raises(MetricsQueryError):
                await prometheus.query("up")
            assert PrometheusConnector.is_connected is True

    @pytest.mark.asyncio
    async def test_both_backends_evaluate_at_the_moment_they_are_given(
        self, prometheus: PrometheusConnector, grafana: GrafanaPrometheusConnector
    ) -> None:
        """``at`` reaches Prometheus as the ``time`` parameter and Grafana as the window end."""
        moment = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

        await prometheus.query("up", at=moment)
        assert prometheus.prom.custom_query.call_args.kwargs["params"] == {"time": moment.timestamp()}

        sent: list[dict[str, Any]] = []
        grafana._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _record(request, sent, grafana_frames))
        )
        await grafana.query("up", at=moment)
        assert sent[0]["to"] == str(int(moment.timestamp() * 1000))

    @pytest.mark.asyncio
    async def test_prometheus_refuses_a_datasource_uid(self, prometheus: PrometheusConnector) -> None:
        """The billing datasource is a Grafana notion; the direct connector has one store."""
        with pytest.raises(ValueError, match="only supported on the Grafana"):
            await prometheus.query("up", datasource_uid="billing-uid")

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(500, MetricsUnavailableError), (401, MetricsUnavailableError), (403, MetricsUnavailableError)],
    )
    @pytest.mark.asyncio
    async def test_grafana_treats_5xx_and_auth_as_unavailable(
        self, grafana: GrafanaPrometheusConnector, status: int, expected: type[Exception]
    ) -> None:
        grafana._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"message": "nope"}))
        )

        with pytest.raises(expected):
            await grafana.query("up")
        assert GrafanaPrometheusConnector.is_connected is False

    @pytest.mark.asyncio
    async def test_grafana_treats_a_transport_error_as_unavailable(self, grafana: GrafanaPrometheusConnector) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        grafana._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

        with pytest.raises(MetricsUnavailableError):
            await grafana.query("up")
        assert GrafanaPrometheusConnector.is_connected is False

    @pytest.mark.asyncio
    async def test_grafana_keeps_a_400_as_a_query_error(self, grafana: GrafanaPrometheusConnector) -> None:
        """A rejected query is the caller's problem, so availability stays as it was."""
        grafana._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"message": "bad expr"}))
        )

        with pytest.raises(MetricsQueryError):
            await grafana.query("up(")
        assert GrafanaPrometheusConnector.is_connected is True

    @pytest.mark.asyncio
    async def test_grafana_returns_nothing_for_an_error_inside_a_200(self, grafana: GrafanaPrometheusConnector) -> None:
        """A datasource failure arrives as a 200 with an error per refId, and raises nothing."""
        body = {"results": {"A": {"error": "datasource timeout", "errorSource": "downstream", "status": 500}}}
        grafana._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))

        assert await grafana.query("up") == []
        assert GrafanaPrometheusConnector.is_connected is True


class TestPrometheusTransport:
    """How the direct connector builds its HTTP client."""

    def test_every_call_has_the_same_timeout_and_verifies_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Queries and the series API used to disagree: no timeout against 10s, off against on."""
        built: dict[str, Any] = {}

        def record(**kwargs: Any) -> MagicMock:
            built.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("opi.connectors.prometheus.PrometheusConnect", record)
        monkeypatch.setattr("opi.core.config.settings.PROMETHEUS_URL", "https://prometheus:9090")
        PrometheusConnector._instance = None
        try:
            PrometheusConnector()
        finally:
            PrometheusConnector._instance = None

        assert built == {"url": "https://prometheus:9090", "timeout": HTTP_TIMEOUT_SECONDS}

    def test_discover_metric_names_returns_sorted_unique_names(
        self, prometheus: PrometheusConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The only backend with this call: it reads the TSDB index, not PromQL."""
        response = MagicMock()
        response.json.return_value = {
            "status": "success",
            "data": [{"__name__": "b_metric"}, {"__name__": "a_metric"}, {"__name__": "a_metric"}],
        }
        monkeypatch.setattr("opi.connectors.prometheus.requests.get", MagicMock(return_value=response))

        assert prometheus.discover_metric_names('{job="minio"}') == ["a_metric", "b_metric"]

    def test_discover_metric_names_turns_a_request_failure_into_the_shared_error(
        self, prometheus: PrometheusConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "opi.connectors.prometheus.requests.get",
            MagicMock(side_effect=requests.ConnectionError("name or service not known")),
        )

        with pytest.raises(MetricsQueryError):
            prometheus.discover_metric_names('{job="minio"}')
