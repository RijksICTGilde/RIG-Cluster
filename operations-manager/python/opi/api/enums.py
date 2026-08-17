"""Enumerations the public API exposes, in one place.

The OpenAPI document is what other teams generate their clients and tools from, so a field
with a fixed set of values has to say so in the schema. It did not: a measurement on
2026-08-04 found five enums in the whole document against 59 string fields whose names
announce a choice (``status``, ``task_type``, ``resource_type``). The values were often
written in the field description instead, where a generator cannot reach them.

Enums live here rather than next to one router because the same set is returned by several
routers, and a second copy is how two sets quietly drift apart. Sets that genuinely differ
per endpoint (a step status carries ``skipped``, a task status does not) stay separate on
purpose; collapsing them into one "status" enum would document values an endpoint never
returns, which is worse than saying nothing.

``StrEnum`` throughout: the members are strings, so typing an existing ``str`` field with
one changes the schema without changing a single runtime value.
"""

from enum import StrEnum


class OperationStatus(StrEnum):
    """Outcome of a backup or restore operation.

    Measured against what the routers actually assign: only these three ever reach a
    ``status`` field. (The ``"error"`` literals elsewhere in those modules are dict keys
    carrying a message, not status values.)
    """

    SUCCESS = "success"
    #: Some resources succeeded and others failed; the response body says which.
    PARTIAL = "partial"
    FAILED = "failed"
