"""What a service change refuses, as exceptions the entrances translate into their own answer.

``error_type`` is the string the API and the task result have always carried, so an
entrance maps an exception onto its answer without a table of its own.
"""

from __future__ import annotations


class ServiceChangeError(Exception):
    """A change that cannot be made as asked. Carries a message meant for the caller."""

    error_type = "unknown"


class InvalidProjectNameError(ServiceChangeError):
    error_type = "invalid_project_name"


class InvalidTargetError(ServiceChangeError):
    error_type = "invalid_target"


class InvalidValuesError(ServiceChangeError):
    error_type = "invalid_values"


class ChangeValidationError(ServiceChangeError):
    error_type = "validation_error"


class ChangeConflictError(ServiceChangeError):
    error_type = "conflict"


#: The ``error_type`` a mutation group reports mapped onto the exception for it. A type
#: this map does not know becomes a plain :class:`ServiceChangeError`.
_BY_ERROR_TYPE: dict[str, type[ServiceChangeError]] = {
    InvalidProjectNameError.error_type: InvalidProjectNameError,
    InvalidTargetError.error_type: InvalidTargetError,
    InvalidValuesError.error_type: InvalidValuesError,
    ChangeValidationError.error_type: ChangeValidationError,
    ChangeConflictError.error_type: ChangeConflictError,
}


def error_from_result(result: dict[str, object], fallback: str) -> ServiceChangeError:
    """The exception for a failed mutation-group result."""
    message = str(result.get("error") or fallback)
    error_type = str(result.get("error_type") or "")
    return _BY_ERROR_TYPE.get(error_type, ServiceChangeError)(message)
