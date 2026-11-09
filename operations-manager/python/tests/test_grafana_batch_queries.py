"""Tests for Grafana Prometheus connector batch query optimization."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from opi.connectors.grafana_prometheus import GrafanaPrometheusConnector
from opi.connectors.metrics_backend import MetricsUnavailableError
from opi.services.metrics_queries import TimeWindow, component_timeseries


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton between tests."""
    GrafanaPrometheusConnector._instance = None
    yield
    GrafanaPrometheusConnector._instance = None


@pytest.fixture
def connector():
    """Create a connected connector with mocked client."""
    with patch.object(GrafanaPrometheusConnector, "__init__", lambda self: None):
        c = GrafanaPrometheusConnector.__new__(GrafanaPrometheusConnector)
        c._initialized = True
        c._grafana_url = "http://grafana:3000"
        c._token = "test-token"
        c._datasource_uid = "test-uid"
        c._datasource_type = "prometheus"
        c._client = AsyncMock(spec=httpx.AsyncClient)
        c._recovery_task = None
        c._recovery_first_delay = 0.01
        c._recovery_max_delay = 0.01
        c._unavailable_since = None
        GrafanaPrometheusConnector.is_connected = True
        GrafanaPrometheusConnector._instance = c
        yield c
        if c._recovery_task is not None:
            c._recovery_task.cancel()


def make_grafana_response(ref_results: dict) -> dict:
    """Build a Grafana API response from refId -> (instant, labels, values) mapping.

    For instant queries: values = [(timestamp_ms, value)]
    For range queries: values = [(ts1, v1), (ts2, v2), ...]
    """
    results = {}
    for ref_id, (instant, labels, values) in ref_results.items():
        if instant:
            timestamps = [v[0] for v in values]
            metric_values = [v[1] for v in values]
            frame = {
                "schema": {"fields": [{}, {"labels": labels}]},
                "data": {"values": [timestamps, metric_values]},
            }
        else:
            timestamps = [v[0] for v in values]
            metric_values = [v[1] for v in values]
            frame = {
                "schema": {"fields": [{}, {"labels": labels}]},
                "data": {"values": [timestamps, metric_values]},
            }
        results[ref_id] = {"frames": [frame]}

    return {"results": results}


class TestExecuteBatchQueries:
    """Tests for _execute_batch_queries."""

    @pytest.mark.asyncio
    async def test_single_query_batch(self, connector):
        """A single-query batch should work like _execute_query."""
        response = make_grafana_response(
            {
                "A": (True, {"pod": "test-pod"}, [(1000000, 0.5)]),
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        connector._client.request = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"A": "up"},
            instant=True,
        )

        assert "A" in results
        assert len(results["A"]) == 1
        assert results["A"][0]["metric"] == {"pod": "test-pod"}
        assert results["A"][0]["value"][1] == "0.5"

        # Verify only 1 HTTP request was made
        assert connector._client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_queries_single_request(self, connector):
        """Multiple queries should be sent in a single HTTP request."""
        response = make_grafana_response(
            {
                "cpu": (True, {"pod": "pod-1"}, [(1000000, 0.25)]),
                "memory": (True, {"pod": "pod-1"}, [(1000000, 134217728)]),
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        connector._client.request = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"cpu": "rate(cpu[5m])", "memory": "container_memory"},
            instant=True,
        )

        assert "cpu" in results
        assert "memory" in results
        assert len(results["cpu"]) == 1
        assert len(results["memory"]) == 1

        # Only 1 HTTP call for 2 queries
        assert connector._client.request.call_count == 1

        # Verify payload contains both queries
        call_kwargs = connector._client.request.call_args
        payload = call_kwargs.kwargs["json"]
        assert len(payload["queries"]) == 2
        ref_ids = {q["refId"] for q in payload["queries"]}
        assert ref_ids == {"cpu", "memory"}

    @pytest.mark.asyncio
    async def test_empty_queries(self, connector):
        """Empty queries dict should return empty results without HTTP call."""
        results = await connector._execute_batch_queries(queries={}, instant=True)
        assert results == {}
        connector._client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_range_batch_queries(self, connector):
        """Range queries should set correct from/to timestamps."""
        response = make_grafana_response(
            {
                "cpu": (False, {}, [(1000000, 0.1), (2000000, 0.2)]),
                "memory": (False, {}, [(1000000, 100), (2000000, 200)]),
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        connector._client.request = AsyncMock(return_value=mock_resp)

        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

        results = await connector._execute_batch_queries(
            queries={"cpu": "rate(cpu[5m])", "memory": "memory_bytes"},
            instant=False,
            start_time=start,
            end_time=end,
            step="5m",
        )

        # Verify from/to are timestamps in ms
        call_kwargs = connector._client.request.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["from"] == str(int(start.timestamp() * 1000))
        assert payload["to"] == str(int(end.timestamp() * 1000))

        # Verify range results have "values" key
        assert "cpu" in results
        assert "memory" in results

    @pytest.mark.asyncio
    async def test_server_error_raises_connection_error(self, connector):
        """A 5xx is the backend failing, not the query: it flips the availability."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        connector._client.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(MetricsUnavailableError, match="HTTP 500"):
            await connector._execute_batch_queries(
                queries={"A": "up"},
                instant=True,
            )

        assert GrafanaPrometheusConnector.is_connected is False

    @pytest.mark.asyncio
    async def test_missing_ref_id_in_response(self, connector):
        """If Grafana doesn't return a refId, its result should be empty list."""
        response = make_grafana_response(
            {
                "cpu": (True, {}, [(1000000, 0.5)]),
                # "memory" not in response
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response
        connector._client.request = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"cpu": "rate(cpu[5m])", "memory": "memory_bytes"},
            instant=True,
        )

        assert len(results["cpu"]) == 1
        assert results["memory"] == []


class TestComponentMetricsTimeseriesBatching:
    """The component time-series takes two Grafana requests, one range and one instant."""

    @pytest.mark.asyncio
    async def test_uses_two_requests(self, connector):
        """Should make exactly 2 HTTP requests: 1 range batch + 1 instant batch."""
        range_response = make_grafana_response(
            {
                "cpu": (False, {}, [(1000000, 0.001), (2000000, 0.002)]),
                "memory": (False, {}, [(1000000, 1048576), (2000000, 2097152)]),
                "network_in": (False, {}, []),
                "network_out": (False, {}, []),
                "disk_read": (False, {}, []),
                "disk_write": (False, {}, []),
            }
        )
        instant_response = make_grafana_response(
            {
                "cpu_limit": (True, {}, [(1000000, 1.0)]),
                "memory_limit": (True, {}, [(1000000, 536870912)]),
                "memory_request": (True, {}, [(1000000, 268435456)]),
            }
        )

        call_count = 0

        async def mock_request(method, url, headers=None, json=None):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            # First call is range, second is instant
            if call_count == 1:
                mock_resp.json.return_value = range_response
            else:
                mock_resp.json.return_value = instant_response
            return mock_resp

        connector._client.request = mock_request

        result = await component_timeseries(connector, "test-ns", "my-pod", TimeWindow())

        # Exactly 2 HTTP requests
        assert call_count == 2

        # Verify CPU data converted to millicores
        assert len(result["cpu"]) == 2
        assert result["cpu"][0]["value"] == round(0.001 * 1000, 2)

        # Verify memory converted to MB
        assert len(result["memory"]) == 2
        assert result["memory"][0]["value"] == round(1048576 / (1024 * 1024), 1)

        # Verify limits
        assert result["cpu_limit"] == round(1.0 * 1000, 0)
        assert result["memory_limit"] == round(536870912 / (1024 * 1024), 0)
        assert result["memory_request"] == round(268435456 / (1024 * 1024), 0)


class TestConvertBatchGrafanaResponse:
    """Test the batch response conversion."""

    def test_converts_multiple_refids(self, connector):
        """Each refId should get its own result list."""
        response = make_grafana_response(
            {
                "A": (True, {"pod": "pod-1"}, [(1000000, 0.5)]),
                "B": (True, {"pod": "pod-2"}, [(2000000, 1.5)]),
            }
        )

        results = connector._convert_batch_grafana_response(response, ref_ids=["A", "B"], instant=True)

        assert results["A"][0]["metric"]["pod"] == "pod-1"
        assert results["A"][0]["value"] == [1000, "0.5"]
        assert results["B"][0]["metric"]["pod"] == "pod-2"
        assert results["B"][0]["value"] == [2000, "1.5"]

    def test_unknown_refid_in_response_ignored(self, connector):
        """Extra refIds in the response that we didn't ask for are ignored."""
        response = {
            "results": {
                "A": {"frames": []},
                "UNKNOWN": {"frames": [{"schema": {"fields": []}, "data": {"values": []}}]},
            }
        }

        results = connector._convert_batch_grafana_response(response, ref_ids=["A"], instant=True)

        assert "A" in results
        assert "UNKNOWN" not in results
