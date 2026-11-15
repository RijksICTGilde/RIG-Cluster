"""EditableVisualizer - UI binding that references an Editable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable, WidgetType


@dataclass
class EditableVisualizer:
    """UI binding: how to visualize an Editable.

    References an ``Editable`` for the data logic (path, validators, converters) and owns
    every rendering concern (widget type, label, description, HTMX).
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
    #: Only shown while EDITING, not while creating.
    #:
    #: For a field about something that already exists. "Markeer voor verwijdering" on a
    #: database schema is the example: it stops managing a schema that is already there and
    #: leaves its data. In the create wizard the schema is still being INVENTED, so asking
    #: whether to stop it is meaningless; you simply leave it out.
    #:
    #: The gate is in ``should_render_editable``; ``readonly_on_edit`` above is the
    #: counterpart, shown but not changeable.
    only_when_editing: bool = False
    locked_by_service: str | None = None
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    children: list[EditableVisualizer] | None = None
