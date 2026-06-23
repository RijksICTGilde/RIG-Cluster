"""Tests for Grafana Prometheus connector batch query optimization."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from opi.connectors.grafana_prometheus import (
    GrafanaPrometheusConnector,
    GrafanaQueryError,
)


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
        GrafanaPrometheusConnector.is_connected = True
        GrafanaPrometheusConnector._instance = c
        return c


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
        connector._client.post = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"A": "up"},
            instant=True,
        )

        assert "A" in results
        assert len(results["A"]) == 1
        assert results["A"][0]["metric"] == {"pod": "test-pod"}
        assert results["A"][0]["value"][1] == "0.5"

        # Verify only 1 HTTP request was made
        assert connector._client.post.call_count == 1

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
        connector._client.post = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"cpu": "rate(cpu[5m])", "memory": "container_memory"},
            instant=True,
        )

        assert "cpu" in results
        assert "memory" in results
        assert len(results["cpu"]) == 1
        assert len(results["memory"]) == 1

        # Only 1 HTTP call for 2 queries
        assert connector._client.post.call_count == 1

        # Verify payload contains both queries
        call_kwargs = connector._client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert len(payload["queries"]) == 2
        ref_ids = {q["refId"] for q in payload["queries"]}
        assert ref_ids == {"cpu", "memory"}

    @pytest.mark.asyncio
    async def test_empty_queries(self, connector):
        """Empty queries dict should return empty results without HTTP call."""
        results = await connector._execute_batch_queries(queries={}, instant=True)
        assert results == {}
        connector._client.post.assert_not_called()

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
        connector._client.post = AsyncMock(return_value=mock_resp)

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
        call_kwargs = connector._client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["from"] == str(int(start.timestamp() * 1000))
        assert payload["to"] == str(int(end.timestamp() * 1000))

        # Verify range results have "values" key
        assert "cpu" in results
        assert "memory" in results

    @pytest.mark.asyncio
    async def test_http_error_raises(self, connector):
        """HTTP errors should raise GrafanaQueryError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        connector._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(GrafanaQueryError, match="status 500"):
            await connector._execute_batch_queries(
                queries={"A": "up"},
                instant=True,
            )

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
        connector._client.post = AsyncMock(return_value=mock_resp)

        results = await connector._execute_batch_queries(
            queries={"cpu": "rate(cpu[5m])", "memory": "memory_bytes"},
            instant=True,
        )

        assert len(results["cpu"]) == 1
        assert results["memory"] == []


class TestComponentMetricsBatching:
    """Test that get_component_metrics uses batched queries."""

    @pytest.mark.asyncio
    async def test_batches_cpu_and_memory(self, connector):
        """CPU and memory should be fetched in a single batched request."""
        batch_response = make_grafana_response(
            {
                "cpu": (True, {}, [(1000000, 0.25)]),
                "memory": (True, {}, [(1000000, 134217728)]),
            }
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = batch_response
        connector._client.post = AsyncMock(return_value=mock_resp)

        result = await connector.get_component_metrics("test-ns", "my-pod", "6h")

        assert result["cpu_cores"] == 0.25
        assert result["memory_bytes"] == 134217728
        assert result["memory_mb"] == 134217728 / (1024 * 1024)

        # 1 batch call for CPU + memory, then 3 sequential HTTP request metric attempts
        # (all return empty from the same mock, so all 3 are tried)
        assert connector._client.post.call_count == 4

    @pytest.mark.asyncio
    async def test_http_requests_metric_tried_sequentially(self, connector):
        """HTTP request metrics should still be tried one by one after the batch."""
        batch_response = make_grafana_response(
            {
                "cpu": (True, {}, [(1000000, 0.1)]),
                "memory": (True, {}, [(1000000, 1048576)]),
            }
        )
        # First call: batch. Second call: http_requests_total (empty). Third: found.
        http_empty = make_grafana_response({"A": (True, {}, [])})
        http_found = make_grafana_response({"A": (True, {}, [(1000000, 42.0)])})

        call_count = 0

        async def mock_post(url, headers=None, json=None):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if call_count == 1:
                mock_resp.json.return_value = batch_response
            elif call_count == 2:
                mock_resp.json.return_value = http_empty
            else:
                mock_resp.json.return_value = http_found
            return mock_resp

        connector._client.post = mock_post

        result = await connector.get_component_metrics("test-ns", "my-pod", "6h")

        assert result["cpu_cores"] == 0.1
        # First try returns empty, second try finds it
        assert result["requests_per_second"] == 42.0
        # 1 batch + up to 3 sequential HTTP metric attempts
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_not_connected_returns_none_values(self, connector):
        """When not connected, should return None values without HTTP calls."""
        GrafanaPrometheusConnector.is_connected = False

        result = await connector.get_component_metrics("test-ns", "my-pod")

        assert result == {
            "cpu_cores": None,
            "memory_bytes": None,
            "memory_mb": None,
            "requests_per_second": None,
        }


class TestComponentMetricsTimeseriesBatching:
    """Test that get_component_metrics_timeseries uses batched queries."""

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

        async def mock_post(url, headers=None, json=None):
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

        connector._client.post = mock_post

        result = await connector.get_component_metrics_timeseries("test-ns", "my-pod", 60, 5)

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


class TestDeploymentParallelization:
    """Test that multi-component methods run in parallel."""

    @pytest.mark.asyncio
    async def test_deployment_metrics_timeseries_parallel(self, connector):
        """Components should be fetched in parallel, not sequentially."""
        call_times: list[float] = []

        original_method = connector.get_component_metrics_timeseries

        async def mock_timeseries(namespace, pod_prefix, duration_minutes=60, step_minutes=5):
            call_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)  # simulate network latency
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

        connector.get_component_metrics_timeseries = mock_timeseries

        result = await connector.get_deployment_component_metrics_timeseries(
            namespace="test-ns",
            components=["web", "api", "worker"],
            deployment_name="my-app",
            duration_minutes=60,
            step_minutes=5,
        )

        assert set(result.keys()) == {"web", "api", "worker"}
        # All 3 calls should start nearly simultaneously (within 20ms of each other)
        assert len(call_times) == 3
        assert max(call_times) - min(call_times) < 0.02

    @pytest.mark.asyncio
    async def test_discovered_workloads_parallel(self, connector):
        """Discovered workloads should be fetched in parallel."""
        call_count = 0

        async def mock_timeseries(namespace, pod_prefix, duration_minutes=60, step_minutes=5):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
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

        connector.get_component_metrics_timeseries = mock_timeseries

        workloads = [
            {"name": "frontend"},
            {"name": "backend"},
            {"name": ""},  # should be skipped
        ]

        result = await connector.get_discovered_workload_metrics_timeseries(
            namespace="test-ns",
            workloads=workloads,
        )

        assert set(result.keys()) == {"frontend", "backend"}
        assert call_count == 2


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
