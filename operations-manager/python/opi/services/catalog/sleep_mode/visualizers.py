"""Visualizers for the sleep-mode service (project-level config section)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.sleep_mode.editables import (
    SLEEP_AFTER_DEPLOY_EDITABLE,
    SLEEP_AFTER_WAKE_EDITABLE,
    SLEEP_DESCRIPTION_EDITABLE,
    SLEEP_ENABLED_EDITABLE,
    SLEEP_MATCH_EDITABLE,
    SLEEP_TITLE_EDITABLE,
    SLEEP_WAKE_MODE_EDITABLE,
    SLEEP_WAKER_COMPONENT_EDITABLE,
    SLEEP_WAKER_EDITABLE,
)

SLEEP_ENABLED = EditableVisualizer(
    editable=SLEEP_ENABLED_EDITABLE,
    widget=WidgetType.SELECT,
    label="Slaapstand inschakelen",
    help_text="Zet slaapstand aan of uit voor dit project.",
)

SLEEP_WAKE_MODE = EditableVisualizer(
    editable=SLEEP_WAKE_MODE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Wektype",
    help_text="Hoe een slapende deployment weer wordt gestart.",
)

SLEEP_MATCH = EditableVisualizer(
    editable=SLEEP_MATCH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Van toepassing op",
    help_text="Comma-gescheiden. Alleen begint-met (pr-*) of eindigt-met (*-preview), geen andere patronen.",
    placeholder="pr-*, *-preview",
    # Ook voor de API-documentatie: dit veld is een vrije string, dus zonder voorbeeld weet
    # een client wel dat er tekst in moet maar niet dat het om een glob op de deploymentnaam
    # gaat (zie opi/api/openapi_choices.py).
    examples=["pr-*", "*-preview", "acceptatie"],
)

SLEEP_AFTER_DEPLOY = EditableVisualizer(
    editable=SLEEP_AFTER_DEPLOY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Slaap na deploy",
    help_text="Hoe lang na een deploy of image-update de deployment in slaapstand gaat.",
)

SLEEP_AFTER_WAKE = EditableVisualizer(
    editable=SLEEP_AFTER_WAKE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Slaap na wekken",
    help_text="Hoe lang na een wek-actie de deployment weer in slaapstand gaat.",
)

SLEEP_WAKER = EditableVisualizer(
    editable=SLEEP_WAKER_EDITABLE,
    widget=WidgetType.SELECT,
    label="Wekkerpod genereren",
    help_text="Genereer een wekkerpod die de wachtpagina toont zolang de deployment slaapt.",
)

SLEEP_WAKER_COMPONENT = EditableVisualizer(
    editable=SLEEP_WAKER_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Wekker-component",
    help_text="Welk component de wachtpagina serveert. Automatisch = het web-component kiezen.",
)

SLEEP_TITLE = EditableVisualizer(
    editable=SLEEP_TITLE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Titel op de wachtpagina",
    placeholder="Naam van de applicatie",
)

SLEEP_DESCRIPTION = EditableVisualizer(
    editable=SLEEP_DESCRIPTION_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Omschrijving op de wachtpagina",
)

SLEEP_MODE_VISUALIZERS = [
    SLEEP_ENABLED,
    SLEEP_WAKE_MODE,
    SLEEP_MATCH,
    SLEEP_AFTER_DEPLOY,
    SLEEP_AFTER_WAKE,
    SLEEP_WAKER,
    SLEEP_WAKER_COMPONENT,
    SLEEP_TITLE,
    SLEEP_DESCRIPTION,
]
