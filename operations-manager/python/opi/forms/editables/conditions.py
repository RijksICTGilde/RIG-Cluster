"""Editable conditions for controlling deferred field behavior.

Conditions are used with ``defers_to`` on editables to determine
when one field should yield its final value to another field.
"""

from __future__ import annotations

from typing import Any


class SentinelValueCondition:
    """True when the field value equals a sentinel that indicates deferral.

    Used by "select with other" patterns: when the select's value is the
    sentinel (e.g. ``__custom__``), the parent defers to the transient
    text field for its final value.
    """

    def __init__(self, sentinel: str = "__custom__") -> None:
        self.sentinel = sentinel

    def check(self, value: Any) -> bool:
        if not value:
            return False
        return str(value) == self.sentinel
