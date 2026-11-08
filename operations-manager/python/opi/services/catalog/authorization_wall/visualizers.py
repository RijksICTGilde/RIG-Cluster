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

#: See AUTH_WALL_COMPONENT_INFO_EDITABLE for the why. A DISPLAY_CARD without a value renders
#: as a heading plus help text, which is exactly what a signpost needs.
AUTH_WALL_COMPONENT_INFO = EditableVisualizer(
    editable=AUTH_WALL_COMPONENT_INFO_EDITABLE,
    widget=WidgetType.DISPLAY_CARD,
    label="Authorization wall",
    readonly=True,
    # gap md and not the default xs: that one is meant for a card with a value under its
    # label (a key, a tag), where close together is right. This is running text under a
    # heading.
    attributes={"icon": "schild-met-vinkje-erop", "icon_color": "groen", "gap": "md"},
    help_text=(
        "Dit component komt achter een inlogpagina te staan. De instellingen daarvan "
        "gelden voor het hele project en staan op het tabblad Services, bij de kaart "
        "Authorization Wall."
    ),
)
