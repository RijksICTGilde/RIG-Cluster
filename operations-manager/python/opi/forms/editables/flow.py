from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.forms.editables.editable import ProjectEditable
    from opi.forms.editables.section import FormSection


class FlowMode(Enum):
    WIZARD = "wizard"
    TABS = "tabs"


@dataclass
class FormFlow:
    """Composes FormSections into a wizard or tabbed interface."""

    flow_id: str
    title: str
    mode: FlowMode
    sections: list[FormSection] = field(default_factory=list)
    show_review: bool = True
    htmx_base_url: str = ""
    save_per_section: bool = True
    generated_editables: list[ProjectEditable] = field(default_factory=list)
    """Editables with generators — computed at submit time, not rendered in forms."""
