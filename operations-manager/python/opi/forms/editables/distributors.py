"""List distributors — retarget and merge items from a temp structure into existing lists.

A distributor takes a temporary list (produced by a wizard section) where each
item mirrors the final shape, matches items to entries in an existing list by
a key field, and appends a sub-list into the matched entry.

Example: distributing a component reference to deployments.

Wizard section produces (transient):
    [
        {"name": "productie", "components": [{"reference": "api", "image": "..."}]},
        {"name": "staging",   "components": [{"reference": "api", "image": "..."}]},
    ]

ListDistributor matches each item to the corresponding deployment by ``name``
and appends the ``components`` sub-list.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass
class ListDistributor:
    """Distribute items from a temporary list into matching entries of a target list.

    Args:
        source_key: Key in the wizard/section data holding the temp list.
        target_path: Top-level key in the project data (e.g. ``"deployments"``).
        match_field: Field used to match source items to target items (e.g. ``"name"``).
        merge_field: Sub-list within each target to append to (e.g. ``"components"``).
    """

    source_key: str
    target_path: str
    match_field: str
    merge_field: str

    def __call__(self, project_data: dict[str, Any], wizard_data: dict[str, Any]) -> None:
        """Distribute items into the project data. Mutates project_data in place."""
        source_items = wizard_data.get(self.source_key)
        if not isinstance(source_items, list):
            return

        targets = project_data.get(self.target_path)
        if not isinstance(targets, list):
            return

        # Build lookup: match_value -> target dict
        target_lookup: dict[str, dict[str, Any]] = {}
        for target in targets:
            if isinstance(target, dict):
                key = target.get(self.match_field)
                if key is not None:
                    target_lookup[str(key)] = target

        # Distribute
        for source in source_items:
            if not isinstance(source, dict):
                continue
            match_value = source.get(self.match_field)
            if match_value is None:
                continue

            new_items = source.get(self.merge_field)
            if not isinstance(new_items, list) or not new_items:
                continue

            target = target_lookup.get(str(match_value))
            if target is not None:
                target.setdefault(self.merge_field, []).extend(copy.deepcopy(new_items))
