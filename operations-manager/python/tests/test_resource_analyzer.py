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
        # max=400 -> 400*1.25+25 = 525. Current limit=530. Change: (530-525)/530 = 0.9%
        # avg=350 -> 350*1.25+25 = 462.5. Current request=470. Change: (470-462.5)/470 = 1.6%
        # Both below 20% -> None
        result = compute_memory_recommendation(
            max_observed_mb=400,
            avg_observed_mb=350,
            current_limit_mb=530,
            current_request_mb=470,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is None

    def test_above_threshold_returns_recommendation(self):
        # max=100 -> 100*1.25+25 = 150. Current limit=512. Change: 70%
        # avg=80 -> 80*1.25 = 100 (below 100Mi, no +25)
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "150Mi"  # 100 * 1.25 + 25
        assert request == "100Mi"  # 80 * 1.25 = 100 (no +25, below threshold)

    def test_buffer_calculation(self):
        # max=200 -> 200*1.5+25 = 325. avg=150 -> 150*1.5+25 = 250
        result = compute_memory_recommendation(
            max_observed_mb=200,
            avg_observed_mb=150,
            current_limit_mb=512,
            current_request_mb=64,
            buffer_percent=50,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "325Mi"  # 200 * 1.5 + 25
        assert request == "250Mi"  # 150 * 1.5 + 25

    def test_oom_kills_force_increase(self):
        # max=450 -> 450*1.25+25 = 587.5. OOM minimum = 512*1.5 = 768. OOM wins.
        result = compute_memory_recommendation(
            max_observed_mb=450,
            avg_observed_mb=400,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
            has_oom_kills=True,
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "768Mi"
        assert "OOM kills detected" in reason

    def test_request_never_exceeds_limit(self):
        # max=100 -> 100*1.25+25 = 150. avg=120 -> 120*1.25+25 = 175, capped to 150.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=120,
            current_limit_mb=512,
            current_request_mb=64,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "150Mi"
        assert request == "150Mi"

    def test_request_based_on_avg(self):
        # max=300 -> 300*1.25+25 = 400. avg=100 -> 100*1.25+25 = 150.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=100,
            current_limit_mb=512,
            current_request_mb=64,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "400Mi"  # 300 * 1.25 + 25
        assert request == "150Mi"  # 100 * 1.25 + 25
        assert "avg" in reason

    def test_small_app_no_absolute_buffer(self):
        # max=50 -> 50*1.25 = 62.5 (no +25, below 100). avg=40 -> 40*1.25 = 50.
        result = compute_memory_recommendation(
            max_observed_mb=50,
            avg_observed_mb=40,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "63Mi"  # 50 * 1.25 = 62.5, ceil = 63
        assert request == "50Mi"  # 40 * 1.25 = 50

    def test_significant_increase(self):
        # max=300 -> 300*1.25+25 = 400. Current limit=256. Change: 56%.
        # avg=200 -> 200*1.25+25 = 275. Current request=128.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=200,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "400Mi"  # 300 * 1.25 + 25
        assert request == "275Mi"  # 200 * 1.25 + 25

    def test_minimum_memory_enforced(self):
        # Very low usage (5Mi max, 3Mi avg) should be clamped to min_memory_mi
        result = compute_memory_recommendation(
            max_observed_mb=5,
            avg_observed_mb=3,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        # 5 * 1.25 = 6.25Mi, clamped to 25Mi
        assert limit == "25Mi"
        # 3 * 1.25 = 3.75Mi, clamped to 25Mi
        assert request == "25Mi"

    def test_minimum_memory_does_not_affect_higher_values(self):
        # max=100 -> 100*1.25+25 = 150. Well above 25Mi min.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "150Mi"
        assert request == "100Mi"
