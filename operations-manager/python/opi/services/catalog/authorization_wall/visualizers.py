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
    # gap md en niet de standaard xs: die is bedoeld voor een kaart met een waarde onder
    # zijn label (een sleutel, een tag), en dan hoort het dicht op elkaar. Dit is een lopende
    # tekst onder een kop, en die plakte eraan vast.
    attributes={"icon": "schild-met-vinkje-erop", "icon_color": "groen", "gap": "md"},
    help_text=(
        "Dit component komt achter een inlogpagina te staan. De instellingen daarvan "
        "gelden voor het hele project en staan op het tabblad Services, bij de kaart "
        "Authorization Wall."
    ),
)
