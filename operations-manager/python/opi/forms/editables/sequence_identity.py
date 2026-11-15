"""The identity of a row in a sequence, as the submission carries it.

The key of a row is ``reference`` or ``name``, and in an edit form that field is readonly: the
widget renders it ``disabled``, and a disabled field is not sent by the browser. Without a key
the processor has to pair the rows with the stored list by INDEX, and then every row behind a
removed row shifts: the component that stayed gets its neighbour's configuration.

So every rendered row sends its key along as a hidden field, the way a rendered sequence sends
its path (``rendered_sequences.py``), and the processor pairs on that. A hidden key is not data
the user may change: the processor restores the readonly field from the paired row after the
merge, so the key only decides WHICH stored row this row belongs to.
"""

from __future__ import annotations

from typing import Any

#: The fields a row can be recognized by, in order of preference.
IDENTITY_KEYS = ("reference", "name")


def identity_of(item: Any) -> tuple[str, Any] | None:  # noqa: ANN401  # a project-file value
    """The key field and key value of *item*, or None when it has neither."""
    if not isinstance(item, dict):
        return None
    for key in IDENTITY_KEYS:
        value = item.get(key)
        if value:
            return key, value
    return None


def match_by_identity(item: Any, originals: list[Any], index: int) -> Any:  # noqa: ANN401  # a project-file value
    """The row from *originals* that belongs to *item*: by key, else by *index*."""
    identity = identity_of(item)
    if identity is not None:
        key, value = identity
        for original in originals:
            if isinstance(original, dict) and original.get(key) == value:
                return original
    if 0 <= index < len(originals):
        return originals[index]
    return None
