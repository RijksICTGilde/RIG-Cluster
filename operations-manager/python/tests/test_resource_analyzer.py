"""Tests for the resource analyzer service."""

import pytest
from opi.services.resource_analyzer import (
    _k8s_memory_to_mb,
    _mb_to_k8s_memory,
    compute_memory_recommendation,
)


class TestK8sMemoryToMb:
    """Tests for _k8s_memory_to_mb conversion."""

    def test_mi_unit(self):
        assert _k8s_memory_to_mb("512Mi") == 512.0

    def test_gi_unit(self):
        assert _k8s_memory_to_mb("1Gi") == 1024.0

    def test_plain_bytes(self):
        # 256 MiB in bytes = 268435456
        result = _k8s_memory_to_mb("268435456")
        assert abs(result - 256.0) < 0.01

    def test_ki_unit(self):
        result = _k8s_memory_to_mb("262144Ki")
        assert abs(result - 256.0) < 0.01

    def test_decimal_m(self):
        # 500M = 500,000,000 bytes = ~476.84 MiB
        result = _k8s_memory_to_mb("500M")
        assert abs(result - 476.84) < 0.1

    def test_decimal_g(self):
        # 1G = 1,000,000,000 bytes = ~953.67 MiB
        result = _k8s_memory_to_mb("1G")
        assert abs(result - 953.67) < 0.1

    def test_invalid_unit(self):
        with pytest.raises(ValueError, match="Unknown memory unit"):
            _k8s_memory_to_mb("512Ti")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _k8s_memory_to_mb("not-a-number")


class TestMbToK8sMemory:
    """Tests for _mb_to_k8s_memory conversion."""

    def test_rounds_up(self):
        assert _mb_to_k8s_memory(245.3) == "246Mi"

    def test_whole_number(self):
        assert _mb_to_k8s_memory(512.0) == "512Mi"

    def test_minimum_1mi(self):
        assert _mb_to_k8s_memory(0.1) == "1Mi"

    def test_large_value(self):
        assert _mb_to_k8s_memory(2048.0) == "2048Mi"


class TestComputeMemoryRecommendation:
    """Tests for compute_memory_recommendation."""

    def test_within_threshold_returns_none(self):
        # Current limit 512Mi, observed 400Mi -> recommended ~500Mi
        # Change is (512-500)/512 = 2.3%, well below 20% threshold
        result = compute_memory_recommendation(
            max_observed_mb=400,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is None

    def test_above_threshold_returns_recommendation(self):
        # Current limit 512Mi, observed 100Mi -> recommended 125Mi
        # Change is (512-125)/512 = 75%, well above 20% threshold
        result = compute_memory_recommendation(
            max_observed_mb=100,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "125Mi"  # 100 * 1.25 = 125

    def test_buffer_calculation(self):
        result = compute_memory_recommendation(
            max_observed_mb=200,
            current_limit_mb=512,
            current_request_mb=64,
            buffer_percent=50,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "300Mi"  # 200 * 1.5 = 300

    def test_oom_kills_force_increase(self):
        # Even within threshold, OOM kills should force an increase
        result = compute_memory_recommendation(
            max_observed_mb=450,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
            has_oom_kills=True,
        )
        assert result is not None
        limit, request, reason = result
        # OOM minimum = 512 * 1.5 = 768
        # observed + buffer = 450 * 1.25 = 562.5
        # OOM minimum wins
        assert limit == "768Mi"
        assert "OOM kills detected" in reason

    def test_request_at_least_current(self):
        result = compute_memory_recommendation(
            max_observed_mb=100,
            current_limit_mb=512,
            current_request_mb=200,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        _, request, _ = result
        # recommended_request = max(125 * 0.5, 200) = 200
        assert request == "200Mi"

    def test_increase_when_usage_near_limit(self):
        # Current limit 256Mi, observed 240Mi -> recommended 300Mi
        # Change is (300-256)/256 = 17%, below 20% threshold -> None
        result = compute_memory_recommendation(
            max_observed_mb=240,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        # 240*1.25=300, diff=300-256=44, pct=44/256=17.2% < 20%
        assert result is None

    def test_significant_increase(self):
        # Current limit 256Mi, observed 300Mi -> recommended 375Mi
        # Change is (375-256)/256 = 46%, above 20%
        result = compute_memory_recommendation(
            max_observed_mb=300,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, _, reason = result
        assert limit == "375Mi"
