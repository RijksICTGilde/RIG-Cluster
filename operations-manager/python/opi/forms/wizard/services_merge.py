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

from opi.forms.editables.merge import deep_merge_into


def service_name(entry: Any) -> str | None:
    """The service identity for a services-list entry, or None if unrecognised."""
    from opi.services.services import service_entry_name

    return service_entry_name(entry)


def _record_key(entry: dict[str, Any]) -> str | None:
    """The record identity key of *entry* (``name``/``reference``), or None for legacy."""
    if "name" in entry:
        return "name"
    if "reference" in entry:
        return "reference"
    return None


def _absorb(result: list[Any], index: dict[str, int], entry: Any) -> None:
    """Fold one entry into *result*, merging onto the entry that shares its name.

    ``index`` maps service name -> position in *result* and is kept in sync. An entry
    whose name is unrecognisable has no identity to merge on and is appended as-is.
    """
    name = service_name(entry)
    if name is None:
        result.append(copy.deepcopy(entry))
        return
    if name not in index:
        result.append(copy.deepcopy(entry))
        index[name] = len(result) - 1
        return

    i = index[name]
    current = result[i]
    if isinstance(current, dict) and isinstance(entry, dict):
        # Same service, two shapes: the stored side carries the record form
        # ({reference: X, config: ...}) while the form side writes the legacy
        # name-as-key form ({X: {config: ...}}). Deep-merging those as-is fuses
        # them into one dict carrying BOTH shapes -- which is what landed in a
        # real project file. Normalize the legacy side to its partner's record
        # key first; that key being there proves the record form is valid here.
        from opi.services.schema_migration import _normalize_service_entry

        current_key, entry_key = _record_key(current), _record_key(entry)
        if current_key and not entry_key:
            entry = _normalize_service_entry(entry, current_key, keep_attachments_legacy=False)
        elif entry_key and not current_key:
            current = _normalize_service_entry(current, entry_key, keep_attachments_legacy=False)
            result[i] = current
        deep_merge_into(current, entry)
    elif isinstance(entry, dict):
        # current is a bare string; the later entry carries the config dict -> take it.
        result[i] = copy.deepcopy(entry)
    # else: the later entry is a bare string and current already holds the dict -> keep current.


def merge_service_lists(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """Merge two services lists by service name.

    ``existing`` order is preserved; services only in ``incoming`` are appended. For a
    service in both, config is deep-merged (``incoming`` overlays); a bare string is
    promoted to ``incoming``'s dict form when only ``incoming`` carries the config.

    Folding one entry at a time means a name occurring twice *within* a single list
    collapses on the same rules. The picker used to post a name twice (a locked service
    rendered both a disabled checkbox and a hidden input carrying the same value), and
    nothing downstream would have caught it: the duplicate reached the project file as a
    config record plus a stray bare string. That hidden input is gone -- a locked
    checkbox is no longer disabled, so it posts its own value once -- but the collapse
    stays: a selection is a set keyed by name, and this is where that is enforced.
    """
    result: list[Any] = []
    index: dict[str, int] = {}
    for entry in [*existing, *incoming]:
        _absorb(result, index, entry)
    return result


def dedupe_service_list(entries: list[Any]) -> list[Any]:
    """Collapse repeated service names in a single list, first position wins."""
    return merge_service_lists([], entries)
