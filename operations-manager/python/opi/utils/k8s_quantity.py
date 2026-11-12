"""A Kubernetes quantity (``500Mi``, ``1Gi``, ``78m``, ``262144k``) as a number."""

import re
from typing import Final

#: The shape of a Kubernetes quantity in a project file: exactly the suffixes the two
#: parsers below can handle and not one character more. ``$defs/k8s-quantity`` in
#: project_v2.json copies this text, and test_project_schema_validation keeps the two in
#: step. Limits do NOT belong here: those live per cluster (cluster_config) or per service
#: (``QuantitySetting``).
K8S_QUANTITY_PATTERN: Final[str] = r"^[0-9]+(\.[0-9]+)?([munk]|Ki|[MGTP]i?)?\Z"


def parse_k8s_memory_to_mi(value: str) -> float:
    """Convert a Kubernetes memory string to MiB (mebibytes).

    Supports the full set of Kubernetes quantity suffixes: binary (Ki, Mi, Gi,
    Ti, Pi), decimal (k, M, G, T, P - note lowercase k for kilo), and plain
    bytes. The VPA recommender in particular emits decimal forms like "262144k".

    Args:
        value: Kubernetes memory string (e.g., "512Mi", "1Gi", "262144k", "536870912")

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

    mib = 1024 * 1024
    multipliers: dict[str, float] = {
        "": 1 / mib,  # plain bytes
        # Binary (power-of-two) suffixes
        "Ki": 1 / 1024,
        "Mi": 1.0,
        "Gi": 1024.0,
        "Ti": 1024.0 * 1024,
        "Pi": 1024.0 * 1024 * 1024,
        # Decimal (power-of-ten) suffixes - VPA emits these (e.g. "262144k")
        "k": 1e3 / mib,
        "M": 1e6 / mib,
        "G": 1e9 / mib,
        "T": 1e12 / mib,
        "P": 1e15 / mib,
    }

    if unit not in multipliers:
        raise ValueError(f"Unknown memory unit: {unit}")

    return num * multipliers[unit]


def parse_k8s_cpu_to_m(value: str) -> float:
    """Convert a Kubernetes CPU quantity to millicores.

    Supports millicores (`78m`), whole/fractional cores (`1`, `0.5`),
    nanocores (`n`) and microcores (`u`) as emitted by the VPA recommender.

    Next to :func:`parse_k8s_memory_to_mi` rather than in the VPA connector it was
    written for: a Kubernetes quantity is turned into a number in one module, so "is 2
    more than 100m" has a single answer. `opi.connectors.vpa` re-exports it, so every
    existing import keeps working.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    value = value.strip()
    if not value:
        raise ValueError("Empty CPU value")
    if value.endswith("m"):
        return float(value[:-1])
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000
    if value.endswith("u"):
        return float(value[:-1]) / 1000
    return float(value) * 1000
