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


def compute_memory_recommendation(
    max_observed_mb: float,
    avg_observed_mb: float,
    current_limit_mb: float,
    current_request_mb: float,
    buffer_percent: int = 25,
    threshold_percent: int = 20,
    has_oom_kills: bool = False,
    min_memory_mi: int = 25,
    max_memory_mi: int = 1024,
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

    Returns:
        Tuple of (recommended_limit, recommended_request, reason) as K8s strings,
        or None if no change is needed (within threshold)
    """
    buffer_factor = 1 + buffer_percent / 100
    recommended_limit_mb = max_observed_mb * buffer_factor
    recommended_request_mb = avg_observed_mb * buffer_factor

    # For apps using >= 100Mi, add a flat 25Mi buffer for request processing headroom
    if max_observed_mb >= 100:
        recommended_limit_mb += 25
    if avg_observed_mb >= 100:
        recommended_request_mb += 25

    # If OOM kills detected, the actual need is higher than what we observed
    # (pod was killed before reaching true peak).  Use a sliding bump factor:
    # small pods get a larger multiplier because 1.5x of e.g. 25Mi is still
    # too small to survive boot, while large pods only need a modest increase.
    if has_oom_kills:
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

    # Enforce cluster minimum
    recommended_limit_mb = max(recommended_limit_mb, float(min_memory_mi))
    recommended_request_mb = max(recommended_request_mb, float(min_memory_mi))

    # Enforce maximum: auto-tuning should not set limits above this.
    # If the pod needs more, manual intervention is required.
    if recommended_limit_mb > max_memory_mi:
        recommended_limit_mb = float(max_memory_mi)
        recommended_request_mb = min(recommended_request_mb, recommended_limit_mb)

    # Request should never exceed limit
    recommended_request_mb = min(recommended_request_mb, recommended_limit_mb)

    # Collapse request to limit when the gap is < 10% - a tiny difference adds no value
    if recommended_limit_mb > 0:
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

    limit_extra = " + 25Mi headroom" if max_observed_mb >= 100 else ""
    request_extra = " + 25Mi headroom" if avg_observed_mb >= 100 else ""

    if has_oom_kills:
        reason = (
            f"OOM kills detected. Limit: max {max_observed_mb:.0f}Mi "
            f"+ {buffer_percent}%{limit_extra} = {recommended_limit_mb:.0f}Mi "
            f"(OOM safety min {oom_minimum:.0f}Mi, {oom_factor:.1f}x). "
            f"Request: avg {avg_observed_mb:.0f}Mi + {buffer_percent}%{request_extra} = {recommended_request_mb:.0f}Mi"
        )
    else:
        reason = (
            f"Limit: max {max_observed_mb:.0f}Mi + {buffer_percent}%{limit_extra} = {recommended_limit_mb:.0f}Mi. "
            f"Request: avg {avg_observed_mb:.0f}Mi + {buffer_percent}%{request_extra} = {recommended_request_mb:.0f}Mi"
        )

    return recommended_limit, recommended_request, reason
