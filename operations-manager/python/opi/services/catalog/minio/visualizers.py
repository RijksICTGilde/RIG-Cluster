"""Visualizers for the minio-storage service (project-level) -- RC-25."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.minio.editables import MINIO_ENABLE_VERSIONING_EDITABLE

MINIO_ENABLE_VERSIONING = EditableVisualizer(
    editable=MINIO_ENABLE_VERSIONING_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Versiebeheer op de bucket",
    description="Bewaar eerdere versies van objecten.",
    help_text=(
        "Aan: MinIO bewaart oudere versies van een object, zodat een overschrijving of "
        "verwijdering terug te draaien is. Dit kost extra opslag, evenredig met hoe vaak "
        "objecten wijzigen."
    ),
)
