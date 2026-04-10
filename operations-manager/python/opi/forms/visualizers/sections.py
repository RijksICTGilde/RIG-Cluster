from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.forms.editables.editable import EditableEnforcer
    from opi.forms.layout import LayoutElement
    from opi.forms.visualizers.visualizer import EditableVisualizer


@dataclass
class FormSection:
    """Groups related editables into a logical UI section (tab or wizard step).

    A FormSection is a reusable grouping of related editables with its own
    layout, title, and visibility rules. It sits between individual editables
    (field level) and FormFlow (wizard level).
    """

    section_id: str
    title: str
    icon: str | None = None
    description: str | None = None
    editables: list[EditableVisualizer] = field(default_factory=list)
    layout: LayoutElement | list[LayoutElement | str] | None = None
    visible: bool | Callable[[dict[str, Any]], bool] = True
    is_readonly: bool = False
    summary_fn: Callable[[dict[str, Any]], str] | None = None
    enforcer: EditableEnforcer | None = None
    post_save_action: str = "save_only"  # "save_only" or "process_project"
    post_merge: Callable[[dict[str, Any], dict[str, Any]], None] | None = None
    """Optional hook called after the section data is merged into the project.

    Receives ``(project_data, wizard_data)`` and mutates ``project_data``
    in place.  Used for cross-list side effects like distributing a new
    component reference to selected deployments.
    """
