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
            _k8s_memory_to_mb("512Zz")

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
    """Tests for compute_memory_recommendation.

    Tuning (non-OOM) drives the *request* from peak observed usage. The limit
    only follows the request when the two were equal (the untouched default);
    a limit already set to differ from the request is left alone. The OOM path
    is unchanged: it still raises the limit to stop the kills.
    """

    def test_within_threshold_returns_none(self):
        # Coupled 525/525. max=400 -> 400*1.25+25 = 525 -> request unchanged.
        result = compute_memory_recommendation(
            max_observed_mb=400,
            avg_observed_mb=350,
            current_limit_mb=525,
            current_request_mb=525,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is None

    def test_coupled_reduction_drops_both(self):
        # Coupled 512/512. Peak 100 -> request 100*1.25+25 = 150; limit follows.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert request == "150Mi"
        assert limit == "150Mi"  # mirrors the request when they were equal
        assert "max 100Mi" in reason

    def test_frozen_limit_left_untouched(self):
        # Limit (512) already differs from request (256): tune only the request.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=256,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert request == "150Mi"  # 100 * 1.25 + 25
        assert limit == "512Mi"  # unchanged
        assert "unchanged" in reason

    def test_request_based_on_peak_not_average(self):
        # Coupled 512/512. Request uses MAX (300), not avg (100).
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=100,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, reason = result
        assert request == "400Mi"  # 300 * 1.25 + 25
        assert limit == "400Mi"
        assert "max 300Mi" in reason

    def test_buffer_calculation(self):
        # Coupled 512/512, 50% buffer. request = 200*1.5+25 = 325.
        result = compute_memory_recommendation(
            max_observed_mb=200,
            avg_observed_mb=150,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=50,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert request == "325Mi"
        assert limit == "325Mi"

    def test_vpa_source_adds_no_buffer(self):
        # VPA target already carries its own margin: take it as-is, no *1.25, no +25Mi.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=300,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
            source="vpa",
        )
        assert result is not None
        limit, request, reason = result
        assert request == "300Mi"  # target passed through, unbuffered
        assert limit == "300Mi"
        assert "VPA target 300Mi" in reason
        assert "+ 25%" not in reason

    def test_vpa_source_clamped_to_frozen_limit(self):
        # Unbuffered VPA target above a frozen limit -> request clamps to the limit.
        result = compute_memory_recommendation(
            max_observed_mb=400,
            avg_observed_mb=400,
            current_limit_mb=256,
            current_request_mb=64,
            buffer_percent=25,
            threshold_percent=20,
            source="vpa",
        )
        assert result is not None
        limit, request, reason = result
        assert limit == "256Mi"  # frozen, untouched
        assert request == "256Mi"  # target 400 clamped to the limit
        assert "VPA target 400Mi = 256Mi" in reason

    def test_request_never_exceeds_limit(self):
        # Frozen limit 200 < computed request 400 -> request capped to the limit.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=120,
            current_limit_mb=200,
            current_request_mb=64,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "200Mi"  # frozen, untouched
        assert request == "200Mi"  # 300*1.25+25=400 capped to the limit

    def test_small_app_no_absolute_buffer(self):
        # Coupled 256/256. max=50 -> 50*1.25 = 62.5 (no +25, below 100).
        result = compute_memory_recommendation(
            max_observed_mb=50,
            avg_observed_mb=40,
            current_limit_mb=256,
            current_request_mb=256,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert request == "63Mi"  # 62.5 ceil
        assert limit == "63Mi"

    def test_significant_increase_coupled(self):
        # Coupled 256/256. Peak grew: request 300*1.25+25 = 400; limit follows.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=200,
            current_limit_mb=256,
            current_request_mb=256,
            buffer_percent=25,
            threshold_percent=20,
        )
        assert result is not None
        limit, request, _ = result
        assert request == "400Mi"
        assert limit == "400Mi"

    def test_minimum_memory_enforced(self):
        # Coupled 512/512. Very low usage clamped to min_memory_mi.
        result = compute_memory_recommendation(
            max_observed_mb=5,
            avg_observed_mb=3,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
            min_memory_mi=25,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "25Mi"
        assert request == "25Mi"

    def test_minimum_memory_does_not_affect_higher_values(self):
        # Coupled 512/512. max=100 -> 100*1.25+25 = 150. Well above 25Mi min.
        result = compute_memory_recommendation(
            max_observed_mb=100,
            avg_observed_mb=80,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
            min_memory_mi=25,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "150Mi"
        assert request == "150Mi"

    def test_request_capped_at_max_memory_request_mi(self):
        """Request is capped at max_memory_request_mi while the limit can go higher."""
        # Coupled 4096/4096. request 2525 capped to 1024; limit keeps full value.
        result = compute_memory_recommendation(
            max_observed_mb=2000,
            avg_observed_mb=1500,
            current_limit_mb=4096,
            current_request_mb=4096,
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
        """Request below the request cap is not affected."""
        # Coupled 512/512. max=300 -> 300*1.25+25 = 400 < 1024 -> no capping.
        result = compute_memory_recommendation(
            max_observed_mb=300,
            avg_observed_mb=200,
            current_limit_mb=512,
            current_request_mb=512,
            buffer_percent=25,
            threshold_percent=20,
            max_memory_mi=4096,
            max_memory_request_mi=1024,
        )
        assert result is not None
        limit, request, _ = result
        assert limit == "400Mi"
        assert request == "400Mi"

    # --- OOM path (unchanged: still raises the limit) ---

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
