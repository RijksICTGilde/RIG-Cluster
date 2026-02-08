from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.forms.editables.editable import EditableEnforcer, ProjectEditable
    from opi.forms.layout import LayoutElement


@dataclass
class EditablePart:
    """Groups related editables into a logical UI section (tab or wizard step)."""

    part_id: str
    title: str
    icon: str | None = None
    description: str | None = None
    editables: list[ProjectEditable] = field(default_factory=list)
    layout: LayoutElement | None = None
    in_create_wizard: bool = True
    wizard_step: int | None = None
    is_readonly: bool = False
    summary_fn: Callable[[dict[str, Any]], str] | None = None
    enforcer: EditableEnforcer | None = None
