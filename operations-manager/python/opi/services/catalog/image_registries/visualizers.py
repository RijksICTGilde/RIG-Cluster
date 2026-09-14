"""Visualizers voor de dienst ``image-registries``."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.image_registries.editables import (
    COMPONENT_REGISTRY_EDITABLE,
    DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
    REGISTRIES_SEQUENCE_EDITABLE,
    REGISTRY_DISPLAY_NAME_EDITABLE,
    REGISTRY_NAME_EDITABLE,
    REGISTRY_PASSWORD_EDITABLE,
    REGISTRY_UPSTREAM_EDITABLE,
    REGISTRY_USERNAME_EDITABLE,
)

REGISTRY_DISPLAY_NAME = EditableVisualizer(
    editable=REGISTRY_DISPLAY_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    help_text=(
        "Hoe je deze registry noemt, bijvoorbeeld Code Overheid. Vrije tekst; wij maken er "
        "zelf een korte verwijzing van. Laat je hem leeg bij een registry die er al is, dan "
        "blijft die verwijzing op het scherm staan."
    ),
)

# Meegestuurd maar niet op het scherm: de slug is de verwijzing vanaf componenten en ligt
# vast zodra hij bestaat. Zie REGISTRY_NAME_EDITABLE voor waarom hij niet weg kan.
REGISTRY_NAME = EditableVisualizer(
    editable=REGISTRY_NAME_EDITABLE,
    widget=WidgetType.HIDDEN,
    label="Verwijzing",
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
    help_text="De gebruikersnaam waarmee ZAD bij je registry inlogt. Zonder gebruikersnaam en token kunnen we je images niet ophalen.",
)

REGISTRY_PASSWORD = EditableVisualizer(
    editable=REGISTRY_PASSWORD_EDITABLE,
    widget=WidgetType.PASSWORD,
    label="Token",
    help_text=(
        "Een token met leesrecht op je packages. Het wordt versleuteld opgeslagen in het projectbestand "
        "en staat hier afgeschermd; wie dit formulier mag openen kan het wel opvragen. Op productie verloopt het token na 90 dagen "
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
    children=[REGISTRY_DISPLAY_NAME, REGISTRY_NAME, REGISTRY_UPSTREAM, REGISTRY_USERNAME, REGISTRY_PASSWORD],
)

COMPONENT_REGISTRY = EditableVisualizer(
    editable=COMPONENT_REGISTRY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Registry",
    help_text=(
        "Waar de image hierboven vandaan komt. Kies je een eigen registry, dan gebruikt dit component "
        "het token daarvan; laat je het op de publieke registry staan, dan wordt er niets opgeslagen. "
        "Voeg een registry toe bij de dienst Eigen container registries."
    ),
)

DEPLOYMENT_COMPONENT_REGISTRY = EditableVisualizer(
    editable=DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Registry",
    help_text="Een andere registry dan het component zelf gebruikt, alleen voor deze deployment.",
)
