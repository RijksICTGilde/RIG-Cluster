"""Which sequences the form actually rendered.

A sequence the user emptied and a sequence the form never showed come back in exactly the same
way: the submission says nothing about either. Writing ``[]`` for both is what the user asked
for in the first case and data loss in the second.

So the form says it itself: every rendered sequence sends a hidden field carrying its own path.
Is the path in that list, then "no items" means emptied. Is it not, then this section was not
about it and the stored value stays.

Does the submission not carry the field at all, then no statement was made (the wizard's final
submission passes the merged project data as "submission", and that knows no form fields), and
the old behaviour holds.
"""

from __future__ import annotations

from typing import Any

#: Name of the hidden field every rendered sequence sends along. With ``[]`` behind it in the
#: form, so json-enc strips the brackets and the several values arrive as a list (the same route
#: ``services[]`` already takes).
RENDERED_SEQUENCES_FIELD = "_gerenderde-reeksen"


def rendered_sequence_paths(submitted: dict[str, Any]) -> set[str] | None:
    """The paths of the sequences this form rendered, or None when it says nothing.

    With a single rendered sequence htmx delivers a string instead of a list; both forms
    arrive here.
    """
    raw = submitted.get(RENDERED_SEQUENCES_FIELD)
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {path for path in raw if isinstance(path, str)}
    return None


def sequence_was_not_drawn(submitted: dict[str, Any], *paths: str) -> bool:
    """Whether the submission says none of *paths* was on the screen.

    *paths* are the forms the same sequence can go by: the real yaml_path and, for a
    virtualized sequence, the path the form posts it under.
    """
    drawn = rendered_sequence_paths(submitted)
    if drawn is None:
        return False
    return not any(path in drawn for path in paths)
