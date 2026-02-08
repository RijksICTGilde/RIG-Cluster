from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.forms.editables.part import EditablePart


class FlowMode(Enum):
    WIZARD = "wizard"
    TABS = "tabs"


@dataclass
class FormFlow:
    """Composes EditableParts into a wizard or tabbed interface."""

    flow_id: str
    title: str
    mode: FlowMode
    parts: list[EditablePart] = field(default_factory=list)
    show_review: bool = True
    htmx_base_url: str = ""
    save_per_part: bool = True
