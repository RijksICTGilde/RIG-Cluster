"""Data-only validation pipeline for Editable objects.

Works on raw Editable instances with no UI dependency: a flat field map against the editable
validators, converters and enforcers, which is what an API endpoint needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from opi.forms.editables.processor import _validator_accepts_context
from opi.forms.editables.validators import RequiredValidator

if TYPE_CHECKING:
    from opi.forms.editables.editable import ContextAwareEditableValidator, Editable, EditableEnforcer


def validate_field(editable: Editable, value: Any, context: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401  # a project-file value
    """The error messages for one value, empty when it is valid.

    ``context`` carries what a validator cannot know from the value alone, such as the ids
    already in use for a uniqueness check, and is passed on only when both given and accepted,
    the same convention as ``EditableFormProcessor._validate_field``.
    """
    errors: list[str] = []

    if editable.required:
        req_errors = RequiredValidator().validate(value)
        if req_errors:
            return req_errors

    if editable.validator and value is not None and value != "":
        # Which call form applies is a property of the validator, not something to discover
        # from a TypeError: that also catches a TypeError raised BY the validator and then
        # silently re-runs it without context. Same shape as FormProcessor.
        if context is not None and _validator_accepts_context(type(editable.validator)):
            errors.extend(cast("ContextAwareEditableValidator", editable.validator).validate(value, context))
        else:
            errors.extend(editable.validator.validate(value))

    return errors


def validate_fields(
    field_map: dict[str, Any],
    editables: dict[str, Editable],
) -> dict[str, list[str]]:
    """The errors per field name; a field without errors is left out.

    A required editable missing from ``field_map`` is checked too.
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
    """Apply ``converter.write()`` to each value, in a new dict.

    A field without a converter, or without an editable, passes through unchanged.
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
    """Run every enforcer against the full *data*, and collect the violations they raise."""
    errors: list[str] = []
    ctx = context or {}

    for enforcer in enforcers:
        try:
            await enforcer.enforce(data, ctx)
        except ValueError as e:
            errors.append(str(e))

    return errors
