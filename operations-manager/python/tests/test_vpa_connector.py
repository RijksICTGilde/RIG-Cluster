"""Tests for VPA recommendation parsing (opi.connectors.vpa)."""

import pytest
from opi.connectors.vpa import parse_k8s_cpu_to_m, parse_vpa_status

# Real status shape captured from an Off-mode VPA on odcn-production.
_VPA_JSON = {
    "status": {
        "conditions": [{"type": "RecommendationProvided", "status": "True"}],
        "recommendation": {
            "containerRecommendations": [
                {
                    "containerName": "app",
                    "lowerBound": {"cpu": "25m", "memory": "350956340"},
                    "target": {"cpu": "78m", "memory": "410771395"},
                    "uncappedTarget": {"cpu": "78m", "memory": "410771395"},
                    "upperBound": {"cpu": "273m", "memory": "552488535"},
                }
            ]
        },
    }
}


class TestParseCpuToM:
    def test_millicores(self):
        assert parse_k8s_cpu_to_m("78m") == 78.0

    def test_whole_core(self):
        assert parse_k8s_cpu_to_m("1") == 1000.0

    def test_fractional_core(self):
        assert parse_k8s_cpu_to_m("0.5") == 500.0

    def test_large_millicores(self):
        assert parse_k8s_cpu_to_m("1500m") == 1500.0

    def test_nanocores(self):
        assert parse_k8s_cpu_to_m("500000000n") == 500.0

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty CPU value"):
            parse_k8s_cpu_to_m("  ")


class TestParseVpaStatus:
    def test_populated(self):
        rec = parse_vpa_status(_VPA_JSON)
        assert rec is not None
        assert rec.container_name == "app"
        assert rec.target_cpu_m == 78.0
        assert abs(rec.target_memory_mi - 391.7) < 0.5
        assert rec.upper_cpu_m == 273.0
        assert abs(rec.upper_memory_mi - 526.9) < 0.5

    def test_empty_status_returns_none(self):
        assert parse_vpa_status({}) is None
        assert parse_vpa_status({"status": {}}) is None

    def test_missing_container_returns_none(self):
        assert parse_vpa_status(_VPA_JSON, container_name="sidecar") is None

    def test_unparseable_value_returns_none(self):
        broken = {
            "status": {
                "recommendation": {
                    "containerRecommendations": [{"containerName": "app", "target": {"cpu": "notacpu", "memory": "1"}}]
                }
            }
        }
        assert parse_vpa_status(broken) is None
