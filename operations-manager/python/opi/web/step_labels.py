"""The Dutch wording of a task's step line, applied when it is SHOWN, not when it is stored.

A handful of step lines are stored values rather than labels: ``Queued`` is the column default
of ``async_tasks.current_step``, and the progress writers store ``Starting...``, ``Done`` and
``Failed``. Translating on display keeps the stored text a technical trace, so the rows that
are already there stay readable.

The API deliberately does NOT translate (``opi/api/task_models.py``): that is a machine
contract, and the zad-cli and the tests read the stored value.
"""

from __future__ import annotations

#: The fixed step lines, stored in English, with the Dutch wording to show.
STEP_LABELS = {
    "Queued": "In wachtrij",
    "Starting...": "Wordt gestart...",
    "Done": "Klaar",
    "Failed": "Mislukt",
}

#: ``Failed: <error>`` is a form older rows still carry. Only the head is translated, the
#: error text passes through unchanged.
_FAILED_PREFIX = "Failed: "


def step_label(step: str | None) -> str | None:
    """The Dutch wording of a stored step line; an unknown line passes through unchanged."""
    if not step:
        return step
    if step in STEP_LABELS:
        return STEP_LABELS[step]
    if step.startswith(_FAILED_PREFIX):
        return "Mislukt: " + step[len(_FAILED_PREFIX) :]
    return step
