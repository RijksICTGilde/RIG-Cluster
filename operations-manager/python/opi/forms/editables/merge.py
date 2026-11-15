"""The one deep merge the form pipeline uses.

The editable processor and the services merge share it. A second copy of a merge rule drifts,
and then a value lands in one path and not in the other.
"""

from __future__ import annotations

import copy
from typing import Any


def deep_merge_into(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay *overlay* onto *base*, in place; a non-dict value replaces.

    Returns *base*, so the call can be used as an expression.
    """
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            deep_merge_into(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base
