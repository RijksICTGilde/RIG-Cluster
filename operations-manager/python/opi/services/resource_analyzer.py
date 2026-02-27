"""
Resource analyzer for computing memory recommendations based on observed usage.

Pure logic module with no I/O — all data is passed in as arguments.
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


def _k8s_memory_to_mb(value: str) -> float:
    """
    Convert a Kubernetes memory string to megabytes.

    Supports: Mi, Gi, M, G, Ki, and plain bytes.

    Args:
        value: Kubernetes memory string (e.g., "512Mi", "1Gi", "536870912")

    Returns:
        Value in megabytes
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
    current_limit_mb: float,
    current_request_mb: float,
    buffer_percent: int = 25,
    threshold_percent: int = 20,
    has_oom_kills: bool = False,
) -> tuple[str, str, str] | None:
    """
    Compute a memory recommendation based on observed usage.

    Args:
        max_observed_mb: Maximum observed memory usage in MB
        current_limit_mb: Current memory limit in MB
        current_request_mb: Current memory request in MB
        buffer_percent: Percentage buffer to add above max observed
        threshold_percent: Only recommend if change exceeds this percentage
        has_oom_kills: Whether OOM kills were detected (forces increase)

    Returns:
        Tuple of (recommended_limit, recommended_request, reason) as K8s strings,
        or None if no change is needed (within threshold)
    """
    recommended_limit_mb = max_observed_mb * (1 + buffer_percent / 100)

    # If OOM kills detected, the actual need is higher than what we observed
    # (pod was killed before reaching true peak). Ensure we at least double
    # the current limit or use observed + buffer, whichever is higher.
    if has_oom_kills:
        oom_minimum = current_limit_mb * 1.5
        recommended_limit_mb = max(recommended_limit_mb, oom_minimum)

    # Request = 50% of limit, but at least the current request
    recommended_request_mb = max(recommended_limit_mb * 0.5, current_request_mb)

    # Check if the change is significant enough
    if not has_oom_kills:
        change_percent = abs(recommended_limit_mb - current_limit_mb) / current_limit_mb * 100
        if change_percent <= threshold_percent:
            return None

    recommended_limit = _mb_to_k8s_memory(recommended_limit_mb)
    recommended_request = _mb_to_k8s_memory(recommended_request_mb)

    if has_oom_kills:
        reason = (
            f"OOM kills detected. Max observed {max_observed_mb:.0f}Mi "
            f"+ {buffer_percent}% buffer = {recommended_limit_mb:.0f}Mi, "
            f"with OOM safety minimum of {current_limit_mb * 1.5:.0f}Mi"
        )
    else:
        reason = f"Max observed {max_observed_mb:.0f}Mi + {buffer_percent}% buffer = {recommended_limit_mb:.0f}Mi"

    return recommended_limit, recommended_request, reason
