"""
Integration tests for metrics API endpoints.

These tests verify the metrics API endpoints work correctly with mocked
Prometheus connector.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_prometheus_connected() -> Any:
    """Mock PrometheusConnector as connected."""
    with patch("opi.api.metrics_router.PrometheusConnector") as mock_class:
        mock_instance = MagicMock()
        mock_instance.is_connected = True
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_prometheus_disconnected() -> Any:
    """Mock PrometheusConnector as disconnected."""
    with patch("opi.api.metrics_router.PrometheusConnector") as mock_class:
        mock_instance = MagicMock()
        mock_instance.is_connected = False
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_prometheus_with_data() -> Any:
    """Mock PrometheusConnector with sample data."""
    with patch("opi.api.metrics_router.PrometheusConnector") as mock_class:
        mock_instance = MagicMock()
        mock_instance.is_connected = True

        # Mock cluster overview
        mock_instance.get_cluster_overview.return_value = {
            "cpu_usage_percent": 45.2,
            "memory_usage_percent": 62.8,
            "pod_count": 25,
            "node_count": 3,
        }

        # Mock CPU usage
        mock_instance.get_cpu_usage_by_namespace.return_value = [
            {"pod": "web-server-1", "namespace": "test-project", "cpu_usage": 0.15},
            {"pod": "api-server-1", "namespace": "test-project", "cpu_usage": 0.08},
        ]

        # Mock memory usage
        mock_instance.get_memory_usage_by_namespace.return_value = [
            {"pod": "web-server-1", "namespace": "test-project", "memory_bytes": 256000000},
            {"pod": "api-server-1", "namespace": "test-project", "memory_bytes": 128000000},
        ]

        # Mock pod count
        mock_instance.get_pod_count.return_value = 15

        # Mock pod restarts
        mock_instance.get_pod_restarts.return_value = [
            {"pod": "web-server-1", "namespace": "test-project", "restarts": 0},
            {"pod": "api-server-1", "namespace": "test-project", "restarts": 2},
        ]

        # Mock network metrics
        mock_instance.get_network_receive_bytes.return_value = [
            {"pod": "web-server-1", "namespace": "test-project", "bytes_rate": 1024000},
        ]
        mock_instance.get_network_transmit_bytes.return_value = [
            {"pod": "web-server-1", "namespace": "test-project", "bytes_rate": 512000},
        ]

        # Mock custom query
        mock_instance.custom_query.return_value = [
            {"metric": {"__name__": "up"}, "value": [1234567890, "1"]},
        ]

        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.mark.integration
class TestMetricsHealthEndpoint:
    """Tests for the metrics health endpoint."""

    def test_health_when_connected(
        self,
        test_client: TestClient,
        mock_prometheus_connected: Any,
    ) -> None:
        """Test health endpoint returns healthy when Prometheus is connected."""
        response = test_client.get("/api/metrics/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["prometheus_connected"] is True

    def test_health_when_disconnected(
        self,
        test_client: TestClient,
        mock_prometheus_disconnected: Any,
    ) -> None:
        """Test health endpoint returns unhealthy when Prometheus is disconnected."""
        response = test_client.get("/api/metrics/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["prometheus_connected"] is False


@pytest.mark.integration
class TestClusterOverviewEndpoint:
    """Tests for the cluster overview endpoint."""

    def test_get_cluster_overview(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting cluster overview metrics."""
        response = test_client.get("/api/metrics/overview")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "data" in data
        assert data["data"]["cpu_usage_percent"] == 45.2
        assert data["data"]["memory_usage_percent"] == 62.8
        assert data["data"]["pod_count"] == 25


@pytest.mark.integration
class TestCPUMetricsEndpoint:
    """Tests for the CPU metrics endpoint."""

    def test_get_cpu_usage(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting CPU usage metrics."""
        response = test_client.get("/api/metrics/cpu")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "data" in data
        assert len(data["data"]) == 2

    def test_get_cpu_usage_with_namespace_filter(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting CPU usage with namespace filter."""
        response = test_client.get("/api/metrics/cpu?namespace=test-project")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["namespace"] == "test-project"


@pytest.mark.integration
class TestMemoryMetricsEndpoint:
    """Tests for the memory metrics endpoint."""

    def test_get_memory_usage(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting memory usage metrics."""
        response = test_client.get("/api/metrics/memory")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "data" in data
        assert len(data["data"]) == 2

    def test_get_memory_usage_with_namespace_filter(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting memory usage with namespace filter."""
        response = test_client.get("/api/metrics/memory?namespace=test-project")
        assert response.status_code == 200
        data = response.json()
        assert data["namespace"] == "test-project"


@pytest.mark.integration
class TestPodMetricsEndpoints:
    """Tests for pod-related metrics endpoints."""

    def test_get_pod_count(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting pod count."""
        response = test_client.get("/api/metrics/pods/count")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["pod_count"] == 15

    def test_get_pod_count_with_namespace(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting pod count with namespace filter."""
        response = test_client.get("/api/metrics/pods/count?namespace=test-project")
        assert response.status_code == 200
        data = response.json()
        assert data["namespace"] == "test-project"

    def test_get_pod_restarts(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting pod restart counts."""
        response = test_client.get("/api/metrics/pods/restarts")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "data" in data
        assert len(data["data"]) == 2


@pytest.mark.integration
class TestNetworkMetricsEndpoint:
    """Tests for network metrics endpoint."""

    def test_get_network_metrics(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test getting network metrics."""
        response = test_client.get("/api/metrics/network")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "data" in data
        assert "receive" in data["data"]
        assert "transmit" in data["data"]


@pytest.mark.integration
class TestCustomQueryEndpoint:
    """Tests for custom PromQL query endpoint."""

    def test_custom_query(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test executing a custom PromQL query."""
        response = test_client.get("/api/metrics/query?query=up")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["query"] == "up"
        assert "data" in data

    def test_custom_query_missing_query_param(
        self,
        test_client: TestClient,
        mock_prometheus_with_data: Any,
    ) -> None:
        """Test that missing query parameter returns 422."""
        response = test_client.get("/api/metrics/query")
        assert response.status_code == 422


@pytest.mark.integration
class TestMetricsErrorHandling:
    """Tests for metrics endpoint error handling."""

    def test_prometheus_connection_error(
        self,
        test_client: TestClient,
    ) -> None:
        """Test handling of Prometheus connection errors."""
        from opi.connectors.prometheus import PrometheusConnectionError

        with patch("opi.api.metrics_router.PrometheusConnector") as mock_class:
            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_instance.get_cluster_overview.side_effect = PrometheusConnectionError("Connection refused")
            mock_class.return_value = mock_instance

            response = test_client.get("/api/metrics/overview")
            assert response.status_code == 503

    def test_prometheus_query_error(
        self,
        test_client: TestClient,
    ) -> None:
        """Test handling of Prometheus query errors."""
        from opi.connectors.prometheus import PrometheusQueryError

        with patch("opi.api.metrics_router.PrometheusConnector") as mock_class:
            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_instance.custom_query.side_effect = PrometheusQueryError("Invalid query")
            mock_class.return_value = mock_instance

            response = test_client.get("/api/metrics/query?query=invalid{}")
            assert response.status_code == 500
