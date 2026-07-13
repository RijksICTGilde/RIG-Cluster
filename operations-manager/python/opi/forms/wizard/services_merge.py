"""Merge project ``services`` lists by service name (identity), not by index.

The services list is a selection set keyed by service name. Each entry is either a
bare string (``"publish-on-web"``) or a single-key dict carrying config
(``{"attachments": {"data": [...]}}``, ``{"keycloak": {"config": {...}}}``).

Wizard sections each contribute a full copy of ``services`` (a section's processed
result is a deepcopy of the whole project data), so a naive index-merge swapped one
service for another and duplicated entries whenever the section lists were misaligned
(e.g. the readonly attachments carrier still holding the pre-edit list). That dropped a
just-added service and produced a duplicate.

Merging by name is order-independent and correct: the same service has its config
deep-merged, a new service is appended, and a service that no active section carries
simply drops out. Config-carrying sections (keycloak/db config, the attachments carrier)
are gated on their own service being selected, so a deselected service's carrier goes
inactive and does not re-add it.
"""

from __future__ import annotations

import copy
from typing import Any


def service_name(entry: Any) -> str | None:
    """The service identity for a services-list entry, or None if unrecognised."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and len(entry) == 1:
        return next(iter(entry))
    return None


def _deep_update(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_update(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def merge_service_lists(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """Merge two services lists by service name.

    ``existing`` order is preserved; services only in ``incoming`` are appended. For a
    service in both, config is deep-merged (``incoming`` overlays); a bare string is
    promoted to ``incoming``'s dict form when only ``incoming`` carries the config.
    """
    result: list[Any] = [copy.deepcopy(entry) for entry in existing]
    index: dict[str, int] = {}
    for i, entry in enumerate(result):
        name = service_name(entry)
        if name is not None:
            index[name] = i

    for entry in incoming:
        name = service_name(entry)
        if name is None:
            result.append(copy.deepcopy(entry))
            continue
        if name in index:
            i = index[name]
            current = result[i]
            if isinstance(current, dict) and isinstance(entry, dict):
                _deep_update(current, entry)
            elif isinstance(entry, dict):
                # current is a bare string; incoming carries the config dict -> take it.
                result[i] = copy.deepcopy(entry)
            # else: incoming is a bare string and current already holds the dict -> keep current.
        else:
            result.append(copy.deepcopy(entry))
            index[name] = len(result) - 1
    return result
