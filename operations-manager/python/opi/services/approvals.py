"""Generic, catalog-driven approver interface (RC-5).

The admin approval flow used to hard-code one subsystem -- it read the project's
``domains:`` block to list pending items and wrote verdicts straight back into it.
This module makes that generic: it iterates the service catalog's :class:`ApprovalSpec`
declarations, so *any* service that declares approvals (today only publish-on-web's
domain / subdomain) surfaces in the same approver UI with no router change.

Two operations, mirroring the spec's ``list_items`` / ``record`` callbacks:

- :func:`collect_approval_items` -- LIST: the flat list of approvable items in a
  project, tagged with their owning service + spec so a verdict can be routed back.
- :func:`apply_approval_verdicts` -- RECORD: apply the approver's decisions, building
  the uniform history entry once and delegating the state write to each item's spec.

State still lives in the root ``domains:`` block for now; moving it under the owning
service is a separate step (see features/futures/service-vertical-slice.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.services.registry import approval_services

if TYPE_CHECKING:
    from opi.services.catalog.approval import ApprovalItem, ApprovalSpec


def _spec_indexes() -> tuple[dict[tuple[str, str], ApprovalSpec], dict[str, ApprovalSpec]]:
    """Build two routing maps: ``(service, key)`` -> spec (exact), and ``key`` -> spec
    (fallback for items that lack a ``service`` tag, e.g. an in-flight modal session
    from before the tag existed). First declaration wins on a bare-key collision.
    """
    by_full: dict[tuple[str, str], ApprovalSpec] = {}
    by_key: dict[str, ApprovalSpec] = {}
    for service in approval_services():
        for spec in service.approval_specs():
            by_full[(service.service_type.value, spec.key)] = spec
            by_key.setdefault(spec.key, spec)
    return by_full, by_key


def collect_approval_items(project_data: dict[str, Any]) -> list[ApprovalItem]:
    """All approvable items currently in ``project_data``, across the catalog.

    Each item is tagged with ``service`` (the owning ServiceType value) and ``type``
    (the spec key) so :func:`apply_approval_verdicts` can route the verdict back to the
    spec that produced it. Specs without a ``list_items`` (check-only) contribute none.
    """
    items: list[ApprovalItem] = []
    for service in approval_services():
        for spec in service.approval_specs():
            if spec.list_items is None:
                continue
            for item in spec.list_items(project_data):
                item.setdefault("service", service.service_type.value)
                item.setdefault("type", spec.key)
                items.append(item)
    return items


def apply_approval_verdicts(
    project_data: dict[str, Any],
    items: list[ApprovalItem],
    admin_email: str = "admin",
) -> None:
    """Apply approver verdicts onto ``project_data`` (mutates in place).

    For each item with a decision (``status`` != ``"skip"``), builds the uniform
    history entry and delegates the state write to the owning spec's ``record``. Items
    left on ``"skip"``, unroutable, or owned by a spec that is not approver-writable are
    ignored -- exactly the previous behaviour, now catalog-routed instead of a
    hard-coded domain/subdomain switch.
    """
    by_full, by_key = _spec_indexes()
    for item in items:
        if not isinstance(item, dict):
            continue
        new_status = item.get("status", "skip")
        if new_status == "skip":
            continue

        spec = by_full.get((item.get("service", ""), item.get("type", ""))) or by_key.get(item.get("type", ""))
        if spec is None or spec.record is None:
            continue

        history_entry: dict[str, str] = {
            "date": datetime.now(UTC).isoformat(),
            "status": new_status,
        }
        if admin_email:
            history_entry["by"] = admin_email
        message = item.get("message") or None
        if message:
            history_entry["message"] = message

        spec.record(project_data, item, history_entry)
