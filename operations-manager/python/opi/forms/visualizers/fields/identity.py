"""Identity visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.identity import (
    CLUSTERS_EDITABLE,
    DESCRIPTION_EDITABLE,
    DISPLAY_NAME_EDITABLE,
    SOURCE_CODE_EDITABLE,
    SOURCE_CODE_NAME_EDITABLE,
    SOURCE_CODE_URL_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

DISPLAY_NAME = EditableVisualizer(
    editable=DISPLAY_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Weergavenaam",
    description="Een beschrijvende naam voor je project",
)

DESCRIPTION = EditableVisualizer(
    editable=DESCRIPTION_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Projectomschrijving",
    description="Korte beschrijving van het doel en de scope van het project",
)

SOURCE_CODE_URL = EditableVisualizer(
    editable=SOURCE_CODE_URL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Link",
)

SOURCE_CODE_NAME = EditableVisualizer(
    editable=SOURCE_CODE_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    description="Optioneel. Zonder naam toont het portaal de link zelf.",
)

SOURCE_CODE = EditableVisualizer(
    editable=SOURCE_CODE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Broncode",
    description=(
        "Links naar de repositories met de code van dit project. Alleen ter informatie: ZAD doet er niets mee."
    ),
    children=[SOURCE_CODE_URL, SOURCE_CODE_NAME],
)

CLUSTERS = EditableVisualizer(
    editable=CLUSTERS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Cluster",
    description="Selecteer het cluster waar dit project op draait",
)
