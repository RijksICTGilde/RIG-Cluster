"""The one deep merge the form pipeline uses.

There used to be two, line for line the same: one in the editable processor
and one in the services merge. Two copies of a merge rule is one copy too
many - the moment they drift, a value lands in one path and not in the other.
"""

from __future__ import annotations

import copy
from typing import Any


def deep_merge_into(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay *overlay* onto *base*, in place. Non-dict values replace.

    Returns *base* so the call can be used as an expression; callers that
    only want the side effect can ignore the return value.
    """
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            deep_merge_into(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base
