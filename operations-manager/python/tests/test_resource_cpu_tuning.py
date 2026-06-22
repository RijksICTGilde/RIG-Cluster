"""Tests for CPU recommendation and the asymmetric deviation gate."""

from opi.connectors.vpa import parse_k8s_cpu_to_m
from opi.services.resource_analyzer import compute_cpu_recommendation, passes_deviation_gate


class TestComputeCpuRecommendation:
    def test_request_tracks_target_plus_buffer(self):
        # Default deployment: limit 500m != request 50m -> limit frozen.
        limit, request, _ = compute_cpu_recommendation(target_cpu_m=78, current_limit_m=500, current_request_m=50)
        # 78 * 1.25 = 97.5 -> 98m, well under the 250m cap
        assert request == "98m"
        # Frozen limit stays put
        assert limit == "500m"

    def test_request_capped_at_max(self):
        _, request, _ = compute_cpu_recommendation(
            target_cpu_m=1000, current_limit_m=500, current_request_m=50, max_cpu_request_m=250
        )
        assert request == "250m"

    def test_limit_capped_at_max_when_not_frozen(self):
        # Equal current limit/request -> not frozen, limit follows request, then capped.
        limit, request, _ = compute_cpu_recommendation(
            target_cpu_m=5000,
            current_limit_m=100,
            current_request_m=100,
            max_cpu_request_m=250,
            max_cpu_limit_m=4000,
        )
        assert parse_k8s_cpu_to_m(limit) == 4000.0
        assert request == "250m"

    def test_min_floor(self):
        _, request, _ = compute_cpu_recommendation(
            target_cpu_m=5, current_limit_m=500, current_request_m=50, min_cpu_m=25
        )
        assert request == "25m"

    def test_request_never_exceeds_limit(self):
        # Not frozen (equal), small target -> limit equals request.
        limit, request, _ = compute_cpu_recommendation(target_cpu_m=40, current_limit_m=40, current_request_m=40)
        assert parse_k8s_cpu_to_m(request) <= parse_k8s_cpu_to_m(limit)


class TestPassesDeviationGate:
    def test_increase_above_threshold(self):
        assert passes_deviation_gate(100, 112, 10, 30) is True

    def test_increase_below_threshold(self):
        assert passes_deviation_gate(100, 108, 10, 30) is False

    def test_decrease_above_threshold(self):
        assert passes_deviation_gate(100, 65, 10, 30) is True

    def test_decrease_below_threshold(self):
        # 25% decrease, below the 30% decrease threshold -> skip
        assert passes_deviation_gate(100, 75, 10, 30) is False

    def test_decrease_uses_larger_threshold_than_increase(self):
        # A 15% move: applied as an increase, skipped as a decrease.
        assert passes_deviation_gate(100, 115, 10, 30) is True
        assert passes_deviation_gate(100, 85, 10, 30) is False

    def test_zero_current_always_applies(self):
        assert passes_deviation_gate(0, 50, 10, 30) is True

    def test_absolute_floor_blocks_small_change(self):
        # 100 -> 130 is a 30% increase (clears the % gate) but only +30 abs;
        # with a 50-unit floor it must be ignored.
        assert passes_deviation_gate(100, 130, 10, 30, min_abs_delta=50) is False
        # Same change with a smaller floor passes.
        assert passes_deviation_gate(100, 130, 10, 30, min_abs_delta=20) is True

    def test_absolute_floor_protects_tiny_pods(self):
        # 25Mi -> 28Mi is 12% (clears the % gate) but only 3Mi - a 16Mi floor
        # stops it churning near the minimum.
        assert passes_deviation_gate(25, 28, 10, 30, min_abs_delta=16) is False
