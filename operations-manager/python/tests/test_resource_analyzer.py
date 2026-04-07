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
            min_memory_mi=25,
        )
        assert result is not None
        limit, request, _ = result
        # 5 * 1.25 = 6.25Mi, clamped to 25Mi
        assert limit == "25Mi"
        # 3 * 1.25 = 3.75Mi, clamped to 25Mi
        assert request == "25Mi"

    def test_oom_with_zero_observed_uses_current_limit(self):
        """When OOM kills happen on startup (no metrics), caller passes current limits
        as observed values. The 2x OOM multiplier (< 256Mi) should produce limit = 128 * 2 = 256Mi."""
        result = compute_memory_recommendation(
            max_observed_mb=128,  # current limit used as baseline
            avg_observed_mb=64,  # current request used as baseline
            current_limit_mb=128,
            current_request_mb=64,
            buffer_percent=25,
            threshold_percent=20,
            has_oom_kills=True,
            min_memory_mi=25,
        )
        assert result is not None
        limit, request, reason = result
        # OOM minimum = 128 * 2.0 = 256. observed+buffer = 128 * 1.25 + 25 = 185. OOM wins.
        assert limit == "256Mi"
        assert "OOM kills detected" in reason

    def test_collapse_request_to_limit_when_close(self):
        """When request is within 10% of limit, they should be collapsed to the same value."""
        # max=80 -> 80*1.25 = 100 (no +25, below 100). avg=76 -> 76*1.25 = 95.
        # Gap: (100-95)/100 = 5% < 10% -> collapsed to 100.
        result = compute_memory_recommendation(
            max_observed_mb=80,
            avg_observed_mb=76,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "100Mi"
        assert request == "100Mi"  # collapsed from 95Mi

    def test_no_collapse_when_gap_large(self):
        """When request is more than 10% below limit, they should stay separate."""
        # max=80 -> 80*1.25 = 100. avg=64 -> 64*1.25 = 80.
        # Gap: (100-80)/100 = 20% >= 10% -> NOT collapsed.
        result = compute_memory_recommendation(
            max_observed_mb=80,
            avg_observed_mb=64,
            current_limit_mb=256,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "100Mi"
        assert request == "80Mi"  # stays separate

    def test_collapse_large_values(self):
        """Collapse also works for larger memory values (~7% gap)."""
        # max=800 -> 800*1.25+25 = 1025. avg=760 -> 760*1.25+25 = 975.
        # Gap: (1025-975)/1025 = 4.9% < 10% -> collapsed.
        result = compute_memory_recommendation(
            max_observed_mb=800,
            avg_observed_mb=760,
            current_limit_mb=2048,
            current_request_mb=1024,
            buffer_percent=25,
            threshold_percent=20,
            max_memory_mi=2048,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "1025Mi"
        assert request == "1025Mi"  # collapsed from 975Mi

    def test_minimum_memory_does_not_affect_higher_values(self):
        # max=100 -> 100*1.25+25 = 150. Well above 25Mi min.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
            min_memory_mi=25,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "150Mi"
        assert request == "100Mi"

    def test_request_capped_at_max_memory_request_mi(self):
        """Request should be capped at max_memory_request_mi while limit can go higher."""
        # max=2000 -> 2000*1.25+25 = 2525. avg=1500 -> 1500*1.25+25 = 1900.
        # Request 1900 > 1024 request cap -> capped to 1024.
        result = compute_memory_recommendation(
            max_observed_mb=2000,
            avg_observed_mb=1500,
            current_limit_mb=4096,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
            max_memory_mi=4096,
            max_memory_request_mi=1024,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "2525Mi"
        assert request == "1024Mi"

    def test_request_not_capped_when_below_max_request(self):
        """Request below the request cap should not be affected."""
        # max=300 -> 300*1.25+25 = 400. avg=200 -> 200*1.25+25 = 275.
        # 275 < 1024 -> no capping.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=200,
            current_limit_mb=512,
            current_request_mb=128,
            buffer_percent=25,
            threshold_percent=20,
            max_memory_mi=4096,
            max_memory_request_mi=1024,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "400Mi"
        assert request == "275Mi"

    def test_oom_request_capped_at_max_request(self):
        """OOM bump should not push request above max_memory_request_mi."""
        # current_limit=2048, OOM factor 1.5x -> oom_minimum=3072.
        # ratio = 1024/2048 = 0.5. request = 3072*0.5 = 1536, capped to 1024.
        result = compute_memory_recommendation(
            max_observed_mb=2048,
            avg_observed_mb=1024,
            current_limit_mb=2048,
            current_request_mb=1024,
            buffer_percent=25,
            threshold_percent=20,
            has_oom_kills=True,
            max_memory_mi=4096,
            max_memory_request_mi=1024,
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "3072Mi"
        assert request == "1024Mi"
        assert "OOM kills detected" in reason

    def test_no_collapse_when_request_at_cap_but_limit_higher(self):
        """Don't collapse request to limit when request is at its cap."""
        # max=800 -> 800*1.25+25 = 1025. avg=760 -> 760*1.25+25 = 975.
        # With max_memory_request_mi=1024: request stays at 975 (below cap).
        # Limit 1025 > 1024 request cap, so no collapse even though gap is <10%.
        result = compute_memory_recommendation(
            max_observed_mb=800,
            avg_observed_mb=760,
            current_limit_mb=2048,
            current_request_mb=1024,
            buffer_percent=25,
            threshold_percent=20,
            max_memory_mi=4096,
            max_memory_request_mi=1024,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "1025Mi"
        assert request == "975Mi"  # NOT collapsed because limit > request cap
