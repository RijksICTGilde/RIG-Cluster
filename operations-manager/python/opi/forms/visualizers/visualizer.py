"""EditableVisualizer - UI binding that references an Editable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable, WidgetType


@dataclass
class EditableVisualizer:
    """UI binding - how to visualize an Editable.

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
    #: Alleen tonen als er BEWERKT wordt, niet bij het aanmaken.
    #:
    #: Voor velden die over iets gaan dat al bestaat. "Markeer voor verwijdering" bij een
    #: databaseschema is het voorbeeld: dat stopt het beheer van een schema dat er al is en
    #: laat de data staan. In de aanmaakwizard sta je een schema te VERZINNEN, en dan is de
    #: vraag of je het wilt stoppen zinloos - je laat het gewoon weg.
    #:
    #: De poort staat in ``should_render_editable``; readonly_on_edit hierboven is de
    #: tegenhanger (wel tonen, niet aanpasbaar).
    alleen_bij_bewerken: bool = False
    locked_by_service: str | None = None
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    children: list[EditableVisualizer] | None = None
