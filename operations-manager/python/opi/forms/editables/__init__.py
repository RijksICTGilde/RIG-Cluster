"""Editable-driven dynamic forms - declarative YAML path -> widget mapping."""

from __future__ import annotations

from opi.forms.editables.editable import (
    Editable,
    EditableConverter,
    EditableEnforcer,
    EditableValidator,
    WidgetType,
)
from opi.forms.editables.part import EditablePart  # backward compat
from opi.forms.editables.path import get_value, resolve_path, set_value

__all__ = [
    "Editable",
    "EditableConverter",
    "EditableEnforcer",
    "EditablePart",
    "EditableValidator",
    "WidgetType",
    "get_value",
    "resolve_path",
    "set_value",
]
