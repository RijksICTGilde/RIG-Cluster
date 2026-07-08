"""Regression: a section enforcer's FieldWarning must be surfaced via the
``field_warnings`` dict, not silently swallowed.

The modal-edit wizard relies on this so an "op aanvraag" subdomein is shown to
the user (and logged) instead of the step re-rendering with no explanation.
"""

import pytest
from opi.forms.editables.enforcers import FieldWarning
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.sections import FormSection


class _WarnEnforcer:
    """Minimal section enforcer that always warns."""

    async def enforce(self, value, context):
        raise FieldWarning("deployments[0]/subdomain", "subdomein 'moza' is op aanvraag")


@pytest.mark.asyncio
async def test_field_warning_is_captured_when_dict_passed():
    section = FormSection(section_id="t", title="t", enforcer=_WarnEnforcer())  # type: ignore[arg-type]
    warnings: dict[str, list[str]] = {}

    global_errors = await EditableFormProcessor().enforce_sections({}, [section], field_warnings=warnings)

    # Warning surfaces via field_warnings, and is NOT misreported as a global error.
    assert global_errors == []
    assert warnings == {"deployments[0]/subdomain": ["subdomein 'moza' is op aanvraag"]}


@pytest.mark.asyncio
async def test_field_warning_is_swallowed_without_dict():
    # Documents the trap the wizard fix avoids: omit field_warnings and the
    # warning vanishes entirely (no error, no warning, no signal).
    section = FormSection(section_id="t", title="t", enforcer=_WarnEnforcer())  # type: ignore[arg-type]

    global_errors = await EditableFormProcessor().enforce_sections({}, [section])

    assert global_errors == []
