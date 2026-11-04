"""The storage quota of a registry's proxy organization: chosen per registry, approved above a threshold.

Same rules as the daily mail budget of send-email: up to ``QUOTA_FREE_MAX_GIB`` a project
picks freely, above it the exact number needs a platform administrator, and until then the
registry keeps what it was already allowed.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.approval import config_item_value_approval
from opi.services.catalog.image_registries.config_model import QUOTA_FIELD, QUOTA_FREE_MAX_GIB
from opi.services.catalog.image_registries.resolution import project_registries
from opi.services.services_enums import ServiceType


def _registry(project_data: dict[str, Any], name: str) -> dict[str, Any]:
    return next((r for r in project_registries(project_data) if r.get("name") == name), {})


def _chosen(project_data: dict[str, Any], name: str) -> int | None:
    value = _registry(project_data, name).get(QUOTA_FIELD)
    # bool is an int in Python, and true is not a quota anyone meant to write.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _last_approved(project_data: dict[str, Any], name: str) -> int | None:
    last = QUOTA_APPROVAL.last_approved_value(project_data, name)
    return int(last) if last.isdigit() and last.isascii() else None


def _granted(project_data: dict[str, Any], name: str) -> int:
    """The highest quota this registry may use without a new approval."""
    last = _last_approved(project_data, name)
    return max(QUOTA_FREE_MAX_GIB, last) if last is not None else QUOTA_FREE_MAX_GIB


def _requested(project_data: dict[str, Any], name: str) -> str:
    """The quota an administrator must decide on; empty is no request.

    Lowering within an earlier approved number returns that number, so the approval block
    and with it the granted quota stay. Empty here would drop the block.
    """
    chosen = _chosen(project_data, name)
    if chosen is None:
        return ""
    if chosen > _granted(project_data, name):
        return str(chosen)
    last = _last_approved(project_data, name)
    return "" if last is None else str(last)


def effective_quota_gib(project_data: dict[str, Any], name: str, cluster_default: int) -> int:
    """THE GATE on the write path: the ``limitGiB`` the organization of this registry gets."""
    chosen = _chosen(project_data, name)
    if chosen is None:
        return cluster_default
    if QUOTA_APPROVAL.spec.is_approved(project_data, (name, str(chosen))):
        return chosen
    return min(chosen, _granted(project_data, name))


QUOTA_APPROVAL = config_item_value_approval(
    ServiceType.IMAGE_REGISTRIES,
    field=QUOTA_FIELD,
    label="Registry-quota",
    item_names=lambda project_data: [str(r["name"]) for r in project_registries(project_data)],
    requested_value=_requested,
    activity=lambda name, value: f"Een quota van {value} GiB voor registry {name}",
    consequence=lambda project_data, name: f"De registry houdt voorlopig {_granted(project_data, name)} GiB.",
)
