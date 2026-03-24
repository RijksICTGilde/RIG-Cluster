"""Tests for Prometheus connector parallel query optimization."""

import time
from unittest.mock import MagicMock, patch

import pytest
from opi.connectors.prometheus import PrometheusConnector


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton between tests."""
    PrometheusConnector._instance = None
    yield
    PrometheusConnector._instance = None


@pytest.fixture
def connector():
    """Create a connected connector with mocked prom client."""
    with patch.object(PrometheusConnector, "__init__", lambda self: None):
        c = PrometheusConnector.__new__(PrometheusConnector)
        c._initialized = True
        c._prometheus_url = "http://prometheus:9090"
        c.prom = MagicMock()
        PrometheusConnector.is_connected = True
        PrometheusConnector._instance = c
        return c


class TestDeploymentComponentMetricsParallel:
    """Test that get_deployment_component_metrics runs components in parallel."""

    def test_parallel_execution(self, connector):
        """Components should be queried in parallel via ThreadPoolExecutor."""
        call_times: list[float] = []

        def slow_get_metrics(namespace, pod_prefix, time_range="6h"):
            call_times.append(time.monotonic())
            time.sleep(0.05)
            return {"cpu_cores": 0.1, "memory_bytes": 100, "memory_mb": 0.1, "requests_per_second": None}

        with patch.object(connector, "get_component_metrics", side_effect=slow_get_metrics):
            result = connector.get_deployment_component_metrics(
                namespace="test-ns",
                components=["web", "api", "worker"],
                deployment_name="my-app",
            )

        assert set(result.keys()) == {"web", "api", "worker"}
        # All 3 should start nearly simultaneously
        assert len(call_times) == 3
        assert max(call_times) - min(call_times) < 0.03


class TestDeploymentTimeseriesParallel:
    """Test that get_deployment_component_metrics_timeseries runs in parallel."""

    def test_parallel_execution(self, connector):
        """Timeseries queries should run in parallel."""
        call_times: list[float] = []
        empty_result = {
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

        def slow_timeseries(namespace, pod_prefix, duration_minutes=60, step_minutes=5):
            call_times.append(time.monotonic())
            time.sleep(0.05)
            return empty_result

        with patch.object(connector, "get_component_metrics_timeseries", side_effect=slow_timeseries):
            result = connector.get_deployment_component_metrics_timeseries(
                namespace="test-ns",
                components=["web", "api"],
                deployment_name="my-app",
            )

        assert set(result.keys()) == {"web", "api"}
        assert len(call_times) == 2
        assert max(call_times) - min(call_times) < 0.03


class TestDiscoveredWorkloadsParallel:
    """Test that get_discovered_workload_metrics_timeseries runs in parallel."""

    def test_parallel_execution(self, connector):
        """Workloads should be queried in parallel."""
        call_count = 0
        empty_result = {
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

        def mock_timeseries(namespace, pod_prefix, duration_minutes=60, step_minutes=5):
            nonlocal call_count
            call_count += 1
            time.sleep(0.05)
            return empty_result

        with patch.object(connector, "get_component_metrics_timeseries", side_effect=mock_timeseries):
            result = connector.get_discovered_workload_metrics_timeseries(
                namespace="test-ns",
                workloads=[
                    {"name": "frontend"},
                    {"name": "backend"},
                    {"name": ""},  # skipped
                ],
            )

        assert set(result.keys()) == {"frontend", "backend"}
        assert call_count == 2

    def test_empty_workloads(self, connector):
        """Empty workloads list should return empty dict."""
        result = connector.get_discovered_workload_metrics_timeseries(
            namespace="test-ns",
            workloads=[],
        )
        assert result == {}
