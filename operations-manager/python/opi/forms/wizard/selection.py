"""A list modal that shows only part of its list.

A modal on a list (the components of a project, the components inside a deployment) normally
renders every item. With a selection it renders only the named ones, and on save those are
put back in their place in the stored list.

What is not selected is NOT in the form: not visible, and not as a hidden field either. On
save it comes from the project file as git holds it at that moment, so an item that was not
shown cannot be overwritten or removed.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.services.project_schema import ProjectIntegrityError

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import ListSelectable


def parse_selection(raw: str | None) -> list[str]:
    """The names out of ``?components=a,b``, without empty names and without duplicates."""
    if not raw:
        return []
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def selectable_names(project_data: dict[str, Any], selectable: ListSelectable) -> list[str]:
    """The names the list of *selectable* holds right now."""
    stored = smart_get_value(project_data, selectable.list_path)
    if not isinstance(stored, list):
        return []
    return [
        item.get(selectable.key_field) for item in stored if isinstance(item, dict) and item.get(selectable.key_field)
    ]


def narrow_to_selection(
    project_data: dict[str, Any],
    selectable: ListSelectable,
    names: list[str],
) -> dict[str, Any]:
    """A copy of *project_data* whose list holds only the selected items.

    The order is the project file's, not the URL's.
    """
    data = copy.deepcopy(project_data)
    stored = smart_get_value(data, selectable.list_path)
    if not isinstance(stored, list):
        return data
    chosen = set(names)
    subset = [item for item in stored if isinstance(item, dict) and item.get(selectable.key_field) in chosen]
    smart_set_value(data, selectable.list_path, subset)
    return data


def splice_selection(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    selectable: ListSelectable,
    names: list[str],
) -> None:
    """Put the shown items back in their own place in the stored list.

    The items that were not shown come from *existing_data*, fresh from git, and stay as they
    are in their own place. A shown item returns to its OWN place, paired on ``key_field``:
    what the user removed leaves its place empty, what they added goes at the end. Pairing on
    position shifts the unshown items as soon as the first item of a selection of two drops
    out.

    Refuses the save when a selected name has meanwhile disappeared from the project file:
    the list under the modal has then changed and the form is speaking about an item that is
    no longer there, the same reason as in
    ``guard_target_still_points_at_the_same_item``.
    """
    submitted = smart_get_value(merged_data, selectable.list_path)
    stored = smart_get_value(existing_data, selectable.list_path)
    if not isinstance(submitted, list) or not isinstance(stored, list):
        return

    chosen = set(names)
    found = {
        item.get(selectable.key_field)
        for item in stored
        if isinstance(item, dict) and item.get(selectable.key_field) in chosen
    }
    missing = [name for name in names if name not in found]
    if missing:
        raise ProjectIntegrityError(
            f"'{', '.join(missing)}' staat niet meer in dit project: er is iets gewijzigd terwijl je aan het "
            "bewerken was. Sluit dit scherm en open het opnieuw, dan werk je weer op de huidige lijst."
        )

    shown: dict[Any, Any] = {}
    added: list[Any] = []
    for item in submitted:
        name = item.get(selectable.key_field) if isinstance(item, dict) else None
        if name in chosen and name not in shown:
            shown[name] = item
        else:
            added.append(item)

    result: list[Any] = []
    for item in stored:
        name = item.get(selectable.key_field) if isinstance(item, dict) else None
        if name in chosen:
            if name in shown:
                result.append(shown[name])
        else:
            result.append(item)
    result.extend(added)
    smart_set_value(merged_data, selectable.list_path, result)
