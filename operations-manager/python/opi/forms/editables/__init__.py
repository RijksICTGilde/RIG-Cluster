"""Editable-driven dynamic forms — declarative YAML path -> widget mapping."""

from __future__ import annotations

from opi.forms.editables.bridge import (
    editable_to_form_field,
    resolve_options_for_editable,
    should_render_editable,
)
from opi.forms.editables.editable import (
    EditableConverter,
    EditableEnforcer,
    EditableValidator,
    ProjectEditable,
)
from opi.forms.editables.flow import FlowMode, FormFlow
from opi.forms.editables.part import EditablePart  # backward compat
from opi.forms.editables.path import get_value, resolve_path, set_value
from opi.forms.editables.section import FormSection

__all__ = [
    "EditableConverter",
    "EditableEnforcer",
    "EditablePart",
    "EditableValidator",
    "FlowMode",
    "FormFlow",
    "FormSection",
    "ProjectEditable",
    "editable_to_form_field",
    "get_value",
    "resolve_options_for_editable",
    "resolve_path",
    "set_value",
    "should_render_editable",
]
