"""Enumerations the public API exposes, in one place.

A set that several routers return belongs here. A set that genuinely differs per endpoint (a
step status carries ``skipped``, a task status does not) stays separate: one shared "status"
enum would document values an endpoint never returns. ``StrEnum`` throughout, so typing an
existing ``str`` field with one changes the schema and no runtime value.
"""

from enum import StrEnum


class OperationStatus(StrEnum):
    """Outcome of a backup or restore operation."""

    SUCCESS = "success"
    #: Some resources succeeded and others failed; the response body says which.
    PARTIAL = "partial"
    FAILED = "failed"
