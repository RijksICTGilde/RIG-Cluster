"""Visualizers for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.authorization_wall.editables import (
    AUTH_WALL_BANNER_EDITABLE,
    AUTH_WALL_COMPONENT_INFO_EDITABLE,
)

AUTH_WALL_BANNER = EditableVisualizer(
    editable=AUTH_WALL_BANNER_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Welkomstbanner tekst",
)

#: Zie AUTH_WALL_COMPONENT_INFO_EDITABLE voor het waarom. Een DISPLAY_CARD zonder waarde
#: rendert als kop plus hulptekst, en dat is precies wat een wegwijzer nodig heeft.
AUTH_WALL_COMPONENT_INFO = EditableVisualizer(
    editable=AUTH_WALL_COMPONENT_INFO_EDITABLE,
    widget=WidgetType.DISPLAY_CARD,
    label="Authorization wall",
    readonly=True,
    attributes={"icon": "schild-met-vinkje-erop", "icon_color": "groen"},
    help_text=(
        "Dit component komt achter een inlogpagina te staan. De instellingen daarvan "
        "gelden voor het hele project en staan op het tabblad Services, bij de kaart "
        "Authorization Wall."
    ),
)
