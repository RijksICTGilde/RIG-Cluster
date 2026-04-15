"""Data-only validation pipeline for Editable objects.

Operates on raw Editable instances without any UI dependency.
Designed for API endpoint validation - validates flat field maps
against editable validators, converters, and enforcers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.validators import RequiredValidator

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable, EditableEnforcer


def validate_field(editable: Editable, value: Any) -> list[str]:
    """Validate a single value against an editable's constraints.

    Runs the required check (if editable.required) and then the
    editable's validator (if present).

    Returns:
        List of error message strings (empty = valid).
    """
    errors: list[str] = []

    if editable.required:
        req_errors = RequiredValidator().validate(value)
        if req_errors:
            return req_errors

    if editable.validator and value is not None and value != "":
        errors.extend(editable.validator.validate(value))

    return errors


def validate_fields(
    field_map: dict[str, Any],
    editables: dict[str, Editable],
) -> dict[str, list[str]]:
    """Validate a dict of {field_name: value} against matching editables.

    Only validates fields present in both ``field_map`` and ``editables``.
    Fields in ``editables`` that are required but absent from ``field_map``
    are also checked.

    Returns:
        Dict mapping field names to their error lists.
        Fields with no errors are omitted.
    """
    errors: dict[str, list[str]] = {}

    for field_name, editable in editables.items():
        value = field_map.get(field_name)
        field_errors = validate_field(editable, value)
        if field_errors:
            errors[field_name] = field_errors

    return errors


def convert_fields(
    field_map: dict[str, Any],
    editables: dict[str, Editable],
    context_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply converter.write() to each value in the field map.

    Returns a new dict with converted values. Fields without a converter
    are passed through unchanged. Only processes fields present in both
    ``field_map`` and ``editables``.

    Args:
        field_map: The field values to convert.
        editables: Mapping of field names to Editable instances.
        context_data: Optional full project/session data passed to converters.
    """
    result: dict[str, Any] = {}

    for field_name, value in field_map.items():
        editable = editables.get(field_name)
        if editable and editable.converter and value is not None:
            result[field_name] = editable.converter.write(value, context_data=context_data)
        else:
            result[field_name] = value

    return result


async def enforce_rules(
    data: dict[str, Any],
    enforcers: list[EditableEnforcer],
    context: dict[str, Any] | None = None,
) -> list[str]:
    """Run business rule enforcers against the data.

    Each enforcer receives the full data dict and a context dict.
    Enforcers raise ``ValueError`` on violation.

    Returns:
        List of error message strings from any violations.
    """
    errors: list[str] = []
    ctx = context or {}

    for enforcer in enforcers:
        try:
            await enforcer.enforce(data, ctx)
        except ValueError as e:
            errors.append(str(e))

    return errors
