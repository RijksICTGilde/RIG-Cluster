"""EditableVisualizer — UI binding that references an Editable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable, WidgetType


@dataclass
class EditableVisualizer:
    """UI binding — how to visualize an Editable.

    References an ``Editable`` for data logic (path, validators, converters)
    and owns all rendering concerns (widget type, label, description, HTMX).
    """

    editable: Editable
    widget: WidgetType
    label: str
    description: str | None = None
    placeholder: str | None = None
    help_text: str | None = None
    help_template: str | None = None
    examples: list[str] | None = None
    attributes: dict[str, str] | None = None
    readonly: bool = False
    readonly_on_edit: bool = False
    locked_by_service: str | None = None
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    children: list[EditableVisualizer] | None = None
