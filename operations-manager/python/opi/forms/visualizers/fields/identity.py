"""Identity visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.identity import (
    CLUSTERS_EDITABLE,
    DESCRIPTION_EDITABLE,
    DISPLAY_NAME_EDITABLE,
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

CLUSTERS = EditableVisualizer(
    editable=CLUSTERS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Cluster",
    description="Selecteer het cluster waar dit project op draait",
)
