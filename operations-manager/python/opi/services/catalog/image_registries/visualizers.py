"""Visualizers voor de dienst ``image-registries``."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.image_registries.editables import (
    COMPONENT_REGISTRY_EDITABLE,
    DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
    REGISTRIES_SEQUENCE_EDITABLE,
    REGISTRY_NAME_EDITABLE,
    REGISTRY_PASSWORD_EDITABLE,
    REGISTRY_UPSTREAM_EDITABLE,
    REGISTRY_USERNAME_EDITABLE,
)

REGISTRY_NAME = EditableVisualizer(
    editable=REGISTRY_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    help_text="De naam waarmee je bij een component naar deze registry verwijst.",
)

REGISTRY_UPSTREAM = EditableVisualizer(
    editable=REGISTRY_UPSTREAM_EDITABLE,
    widget=WidgetType.TEXT,
    label="Registry",
    help_text=(
        "De registry waar je images staan, inclusief je eigen pad en zonder protocol, "
        "bijvoorbeeld code.overheid.nl/jouw-naam."
    ),
)

REGISTRY_USERNAME = EditableVisualizer(
    editable=REGISTRY_USERNAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Gebruikersnaam",
    help_text="De gebruikersnaam waarmee ZAD bij je registry inlogt.",
)

REGISTRY_PASSWORD = EditableVisualizer(
    editable=REGISTRY_PASSWORD_EDITABLE,
    widget=WidgetType.TEXT,
    label="Token",
    help_text=(
        "Een token met leesrecht op je packages. Het wordt versleuteld opgeslagen in het projectbestand; "
        "wie dit formulier mag openen ziet het hier weer staan. Op productie verloopt het token na 90 dagen "
        "en moet je het opnieuw invullen."
    ),
)

REGISTRIES_SEQUENCE = EditableVisualizer(
    editable=REGISTRIES_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Eigen registries",
    help_text=(
        "De private registries waaruit dit project images haalt. Voor een publieke image hoef je hier niets "
        "in te vullen."
    ),
    children=[REGISTRY_NAME, REGISTRY_UPSTREAM, REGISTRY_USERNAME, REGISTRY_PASSWORD],
)

COMPONENT_REGISTRY = EditableVisualizer(
    editable=COMPONENT_REGISTRY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Registry",
    help_text=(
        "De eigen registry waar de image van dit component vandaan komt. Kiezen hoeft alleen als meer dan een "
        "van je registries bij deze image past: een registry die je hierboven opgeeft geldt sowieso voor elke "
        "image die eronder valt."
    ),
)

DEPLOYMENT_COMPONENT_REGISTRY = EditableVisualizer(
    editable=DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Registry",
    help_text="Een andere registry dan het component zelf gebruikt, alleen voor deze deployment.",
)
