"""Visualizer van ``seaweedfs-storage`` (projectlaag)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.seaweedfs.editables import SEAWEEDFS_ENABLE_VERSIONING_EDITABLE

SEAWEEDFS_ENABLE_VERSIONING = EditableVisualizer(
    editable=SEAWEEDFS_ENABLE_VERSIONING_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Versiebeheer op de bucket",
    description="Bewaar eerdere versies van objecten.",
    help_text=(
        "Aan: SeaweedFS bewaart oudere versies van een object, zodat een overschrijving of "
        "verwijdering terug te draaien is. Dit kost extra opslag, evenredig met hoe vaak "
        "objecten wijzigen."
    ),
)
