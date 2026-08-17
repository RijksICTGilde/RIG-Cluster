"""Visualizers for the temp-storage service (component-level storage mounts).

Co-located with the service. Consumed by the component-form aggregation in
``opi.forms.visualizers.fields.components`` (which imports these), keeping the
temp-storage UI definition inside the service package.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.temp_storage.editables import (
    TEMP_STORAGE_MOUNT_PATH_EDITABLE,
    TEMP_STORAGE_NAME_EDITABLE,
    TEMP_STORAGE_SEQUENCE_EDITABLE,
    TEMP_STORAGE_SIZE_EDITABLE,
)

TEMP_STORAGE_NAME = EditableVisualizer(
    editable=TEMP_STORAGE_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    help_text="Unieke naam voor dit tijdelijke opslagvolume binnen het component.",
)

TEMP_STORAGE_SIZE = EditableVisualizer(
    editable=TEMP_STORAGE_SIZE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Grootte",
    help_text="De maximale grootte van het tijdelijke opslagvolume.",
)

TEMP_STORAGE_MOUNT_PATH = EditableVisualizer(
    editable=TEMP_STORAGE_MOUNT_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Het pad in de container waar het tijdelijke volume wordt gemount (bijv. /tmp/cache).",
)

TEMP_STORAGE_SEQUENCE = EditableVisualizer(
    editable=TEMP_STORAGE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Tijdelijke opslag",
    help_text="Tijdelijke opslagvolumes die in de container worden gemount (worden gewist bij herstart)",
    children=[TEMP_STORAGE_NAME, TEMP_STORAGE_SIZE, TEMP_STORAGE_MOUNT_PATH],
)
