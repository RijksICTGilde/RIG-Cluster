"""
Resource analyzer for computing memory recommendations based on observed usage.

Pure logic module with no I/O - all data is passed in as arguments.
"""

import logging
import math
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ResourceRecommendation:
    """A recommendation for adjusting a component's resource limits."""

    component: str
    current_limits_memory: str
    recommended_limits_memory: str
    current_requests_memory: str
    recommended_requests_memory: str
    max_observed_memory_mb: float
    has_oom_kills: bool
    reason: str


def parse_k8s_memory_to_mi(value: str) -> float:
    """Convert a Kubernetes memory string to MiB (mebibytes).

    Supports: Mi, Gi, M, G, Ki, and plain bytes.

    Args:
        value: Kubernetes memory string (e.g., "512Mi", "1Gi", "536870912")

    Returns:
        Value in MiB

    Raises:
        ValueError: If the value cannot be parsed or uses an unknown unit.
    """
    value = value.strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$", value)
    if not match:
        raise ValueError(f"Cannot parse memory value: {value}")

    num = float(match.group(1))
    unit = match.group(2)

    multipliers: dict[str, float] = {
        "": 1 / (1024 * 1024),  # plain bytes
        "Ki": 1 / 1024,
        "Mi": 1.0,
        "Gi": 1024.0,
        "M": 1000000 / (1024 * 1024),  # decimal megabytes
        "G": 1000000000 / (1024 * 1024),  # decimal gigabytes
    }

    if unit not in multipliers:
        raise ValueError(f"Unknown memory unit: {unit}")

    return num * multipliers[unit]


def _k8s_memory_to_mb(value: str) -> float:
    """Convert a Kubernetes memory string to megabytes.

    Alias for :func:`parse_k8s_memory_to_mi` (MiB and MB are used
    interchangeably in this codebase since all values are binary).
    """
    return parse_k8s_memory_to_mi(value)


def _mb_to_k8s_memory(mb: float) -> str:
    """
    Convert megabytes to a Kubernetes memory string (Mi).

    Rounds up to the nearest whole Mi value.

    Args:
        mb: Value in megabytes

    Returns:
        Kubernetes memory string (e.g., "384Mi")
    """
    rounded = math.ceil(mb)
    if rounded < 1:
        rounded = 1
    return f"{rounded}Mi"


def _m_to_k8s_cpu(m: float) -> str:
    """Convert millicores to a Kubernetes CPU string, rounding up (min 1m)."""
    rounded = math.ceil(m)
    if rounded < 1:
        rounded = 1
    return f"{rounded}m"


def passes_deviation_gate(
    current: float,
    recommended: float,
    increase_threshold_percent: int,
    decrease_threshold_percent: int,
) -> bool:
    """Asymmetric change gate used to avoid a storm of tiny tuning commits.

    React promptly to increases (reliability), conservatively to decreases
    (cost-only — not worth churning git/ArgoCD for a small reclaim).

    Returns True when the change from ``current`` to ``recommended`` is large
    enough to act on: increases must exceed ``increase_threshold_percent``,
    decreases the (larger) ``decrease_threshold_percent``.
    """
    if current <= 0:
        return True
    deviation = abs(recommended - current) / current * 100
    if recommended >= current:
        return deviation >= increase_threshold_percent
    return deviation >= decrease_threshold_percent


def compute_cpu_recommendation(
    target_cpu_m: float,
    current_limit_m: float,
    current_request_m: float,
    buffer_percent: int = 25,
    min_cpu_m: int = 25,
    max_cpu_request_m: int = 250,
    max_cpu_limit_m: int = 4000,
) -> tuple[str, str, str]:
    """Compute a CPU recommendation from a VPA target.

    CPU is compressible (it throttles, never OOM-kills), so there is no floor
    or emergency path. The request tracks the VPA target plus a buffer, capped
    at ``max_cpu_request_m``. The limit mirrors the request only when the two
    were equal (the untouched default); a deliberately-set limit is left frozen.
    Both are bounded by the cluster CPU ceilings.

    The caller decides whether the change is worth applying (see
    :func:`passes_deviation_gate`); this function always returns the ideal
    clamped values.

    Returns:
        Tuple of (recommended_limit, recommended_request, reason) as K8s strings.
    """
    buffer_factor = 1 + buffer_percent / 100
    # A frozen limit was deliberately set to differ from the request; respect it.
    limit_frozen = current_limit_m != current_request_m

    recommended_request_m = target_cpu_m * buffer_factor
    recommended_limit_m = current_limit_m if limit_frozen else recommended_request_m

    # Enforce cluster minimum
    recommended_request_m = max(recommended_request_m, float(min_cpu_m))
    recommended_limit_m = max(recommended_limit_m, float(min_cpu_m))

    # Enforce ceilings: requests are capped lower than limits
    recommended_request_m = min(recommended_request_m, float(max_cpu_request_m))
    if recommended_limit_m > max_cpu_limit_m:
        recommended_limit_m = float(max_cpu_limit_m)

    # Request should never exceed limit
    recommended_request_m = min(recommended_request_m, recommended_limit_m)

    recommended_limit = _m_to_k8s_cpu(recommended_limit_m)
    recommended_request = _m_to_k8s_cpu(recommended_request_m)
    limit_note = (
        f" Limit unchanged at {recommended_limit_m:.0f}m"
        if limit_frozen
        else f" Limit kept equal at {recommended_limit_m:.0f}m"
    )
    reason = (
        f"CPU request: VPA target {target_cpu_m:.0f}m + {buffer_percent}% "
        f"= {recommended_request_m:.0f}m (cap {max_cpu_request_m}m).{limit_note}"
    )
    return recommended_limit, recommended_request, reason


def compute_memory_recommendation(
    max_observed_mb: float,
    avg_observed_mb: float,
    current_limit_mb: float,
    current_request_mb: float,
    buffer_percent: int = 25,
    threshold_percent: int = 20,
    has_oom_kills: bool = False,
    min_memory_mi: int = 25,
    max_memory_mi: int = 4096,
    max_memory_request_mi: int | None = None,
) -> tuple[str, str, str] | None:
    """
    Compute a memory recommendation based on observed usage.

    Uses avg observed + buffer for requests (typical usage) and
    max observed + buffer for limits (peak protection).

    Args:
        max_observed_mb: Maximum observed memory usage in MB
        avg_observed_mb: Average observed memory usage in MB
        current_limit_mb: Current memory limit in MB
        current_request_mb: Current memory request in MB
        buffer_percent: Percentage buffer to add above observed values
        threshold_percent: Only recommend if change exceeds this percentage
        has_oom_kills: Whether OOM kills were detected (forces increase)
        min_memory_mi: Minimum memory value in Mi enforced by the container runtime
        max_memory_mi: Maximum memory limit in Mi
        max_memory_request_mi: Maximum memory request in Mi (defaults to max_memory_mi)

    Returns:
        Tuple of (recommended_limit, recommended_request, reason) as K8s strings,
        or None if no change is needed (within threshold)
    """
    if max_memory_request_mi is None:
        max_memory_request_mi = max_memory_mi
    buffer_factor = 1 + buffer_percent / 100

    # A frozen limit is one we must not touch during a (non-OOM) reduction:
    # the operator already set it to differ from the request, so we respect it.
    limit_frozen = (not has_oom_kills) and (current_limit_mb != current_request_mb)

    if has_oom_kills:
        # OOM: the limit must grow to stop the kills. Limit tracks peak,
        # request tracks average; the bump below raises the limit when the
        # observed peak alone is not enough.
        recommended_limit_mb = max_observed_mb * buffer_factor
        recommended_request_mb = avg_observed_mb * buffer_factor
        # For apps using >= 100Mi, add a flat 25Mi processing headroom
        if max_observed_mb >= 100:
            recommended_limit_mb += 25
        if avg_observed_mb >= 100:
            recommended_request_mb += 25

        # The actual need is higher than what we observed (pod was killed
        # before reaching true peak).  Use a sliding bump factor: small pods
        # get a larger multiplier because 1.5x of e.g. 25Mi is still too small
        # to survive boot, while large pods only need a modest increase.
        if current_limit_mb < 64:
            oom_factor = 3.0
        elif current_limit_mb < 256:
            oom_factor = 2.0
        else:
            oom_factor = 1.5
        oom_minimum = current_limit_mb * oom_factor
        if oom_minimum > recommended_limit_mb:
            # OOM bump is driving the limit — scale request proportionally
            # to maintain the original request/limit ratio, so the gap
            # doesn't become unreasonably large.
            ratio = current_request_mb / current_limit_mb if current_limit_mb > 0 else 1.0
            recommended_request_mb = max(recommended_request_mb, oom_minimum * ratio)
            recommended_limit_mb = oom_minimum
    else:
        # Reduction / tuning: the request tracks the peak observed usage
        # ("the highest we measured") plus a buffer.  The limit mirrors the
        # request only when the two were equal (the untouched default); when
        # the limit was already set to differ from the request, leave it as-is.
        recommended_request_mb = max_observed_mb * buffer_factor
        if max_observed_mb >= 100:
            recommended_request_mb += 25
        recommended_limit_mb = current_limit_mb if limit_frozen else recommended_request_mb

    # Enforce cluster minimum
    recommended_limit_mb = max(recommended_limit_mb, float(min_memory_mi))
    recommended_request_mb = max(recommended_request_mb, float(min_memory_mi))

    # Enforce maximum: auto-tuning should not set limits above this.
    # If the pod needs more, manual intervention is required.
    if recommended_limit_mb > max_memory_mi:
        recommended_limit_mb = float(max_memory_mi)

    # Cap requests separately — requests have a lower ceiling than limits.
    # Below the request cap, requests and limits scale together.
    # Above it, only limits keep climbing (up to max_memory_mi).
    recommended_request_mb = min(recommended_request_mb, float(max_memory_request_mi))

    # Request should never exceed limit
    recommended_request_mb = min(recommended_request_mb, recommended_limit_mb)

    # Collapse request to limit when both are below the request cap
    # and the gap is < 10% — a tiny difference adds no value.
    # Don't collapse when request is at its cap but limit is higher, and
    # never when the limit is frozen (that would silently raise the request
    # to a limit we were asked to leave alone).
    if not limit_frozen and recommended_limit_mb > 0 and recommended_limit_mb <= max_memory_request_mi:
        gap_ratio = (recommended_limit_mb - recommended_request_mb) / recommended_limit_mb
        if gap_ratio < 0.10:
            recommended_request_mb = recommended_limit_mb

    # Check if the change is significant enough
    if not has_oom_kills:
        limit_change = abs(recommended_limit_mb - current_limit_mb) / current_limit_mb * 100
        request_change = (
            abs(recommended_request_mb - current_request_mb) / current_request_mb * 100
            if current_request_mb > 0
            else 100.0
        )
        if limit_change <= threshold_percent and request_change <= threshold_percent:
            return None

    recommended_limit = _mb_to_k8s_memory(recommended_limit_mb)
    recommended_request = _mb_to_k8s_memory(recommended_request_mb)

    if has_oom_kills:
        limit_extra = " + 25Mi headroom" if max_observed_mb >= 100 else ""
        request_extra = " + 25Mi headroom" if avg_observed_mb >= 100 else ""
        reason = (
            f"OOM kills detected. Limit: max {max_observed_mb:.0f}Mi "
            f"+ {buffer_percent}%{limit_extra} = {recommended_limit_mb:.0f}Mi "
            f"(OOM safety min {oom_minimum:.0f}Mi, {oom_factor:.1f}x). "
            f"Request: avg {avg_observed_mb:.0f}Mi + {buffer_percent}%{request_extra} = {recommended_request_mb:.0f}Mi"
        )
    else:
        request_extra = " + 25Mi headroom" if max_observed_mb >= 100 else ""
        limit_note = (
            f" Limit unchanged at {recommended_limit_mb:.0f}Mi"
            if limit_frozen
            else f" Limit kept equal at {recommended_limit_mb:.0f}Mi"
        )
        reason = (
            f"Request: max {max_observed_mb:.0f}Mi + {buffer_percent}%{request_extra} "
            f"= {recommended_request_mb:.0f}Mi.{limit_note}"
        )

    return recommended_limit, recommended_request, reason
