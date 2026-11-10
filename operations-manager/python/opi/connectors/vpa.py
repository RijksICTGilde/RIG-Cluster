"""Turn a VerticalPodAutoscaler's ``.status.recommendation`` into normalized values.

CPU in millicores, memory in MiB. The kubectl I/O lives in
:meth:`KubectlConnector.get_vpa_recommendation`; this module has none.
"""

import logging
from dataclasses import dataclass
from typing import Any

from opi.services.resource_analyzer import parse_k8s_cpu_to_m, parse_k8s_memory_to_mi

logger = logging.getLogger(__name__)

# parse_k8s_cpu_to_m is re-exported: callers import it from here, not from
# opi/services/resource_analyzer.py where it lives.
__all__ = ["VpaContainerRecommendation", "parse_k8s_cpu_to_m", "parse_vpa_status"]


@dataclass
class VpaContainerRecommendation:
    """`target` is the headline recommendation; `lower`/`upper` form the confidence envelope."""

    container_name: str
    target_cpu_m: float
    target_memory_mi: float
    lower_cpu_m: float
    lower_memory_mi: float
    upper_cpu_m: float
    upper_memory_mi: float


def parse_vpa_status(vpa_json: dict[str, Any], container_name: str = "app") -> VpaContainerRecommendation | None:
    """Extract one container's recommendation from a VPA object's JSON.

    None when the recommender has not populated `.status` yet (a freshly created VPA), when
    the container is absent, or when a value cannot be parsed: all of them mean "no usable
    recommendation, fall back to the other source".
    """
    recommendations = vpa_json.get("status", {}).get("recommendation", {}).get("containerRecommendations", [])
    for entry in recommendations:
        if entry.get("containerName") != container_name:
            continue
        target = entry.get("target", {})
        lower = entry.get("lowerBound", {})
        upper = entry.get("upperBound", {})
        try:
            return VpaContainerRecommendation(
                container_name=container_name,
                target_cpu_m=parse_k8s_cpu_to_m(target["cpu"]),
                target_memory_mi=parse_k8s_memory_to_mi(target["memory"]),
                lower_cpu_m=parse_k8s_cpu_to_m(lower["cpu"]),
                lower_memory_mi=parse_k8s_memory_to_mi(lower["memory"]),
                upper_cpu_m=parse_k8s_cpu_to_m(upper["cpu"]),
                upper_memory_mi=parse_k8s_memory_to_mi(upper["memory"]),
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"Unparseable VPA recommendation for container '{container_name}': {e}")
            return None
    return None
