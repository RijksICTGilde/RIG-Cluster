"""What a change to a project file refuses, as exceptions the entrances translate.

``error_type`` is the string the API answer and the task result have always carried, so an
entrance maps a refusal onto its own answer without a table of its own.
"""

from __future__ import annotations

from typing import Any

#: What an error is when the caller did nothing wrong and we know no more than that.
INTERNAL_ERROR = "internal_error"


class ProjectChangeError(Exception):
    """A change to a project that cannot be made as asked."""

    def __init__(self, message: str, error_type: str = INTERNAL_ERROR) -> None:
        super().__init__(message)
        self.error_type = error_type


class StepUpRequiredError(ProjectChangeError):
    """The change touches production and the caller has no fresh second factor.

    ``handeling`` is the "te"-infinitive for the step-up dialog; the message is the refusal
    text, already recorded in the audit.
    """

    def __init__(self, handeling: str, refusal_text: str) -> None:
        super().__init__(refusal_text, "step_up_required")
        self.handeling = handeling


def error_from_result(result: dict[str, Any], fallback: str) -> ProjectChangeError:
    """The exception for a failed mutation-group result."""
    return ProjectChangeError(str(result.get("error") or fallback), str(result.get("error_type") or INTERNAL_ERROR))
