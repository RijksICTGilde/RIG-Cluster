"""Visualizers for the persistent-storage service (component-level storage mounts).

Co-located with the service. Consumed by the component-form aggregation in
``opi.forms.visualizers.fields.components`` via ``config_component_visualizers()``.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.persistent_storage.editables import (
    PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    PERSISTENT_STORAGE_NAME_EDITABLE,
    PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
    PERSISTENT_STORAGE_SIZE_EDITABLE,
)

PERSISTENT_STORAGE_NAME = EditableVisualizer(
    editable=PERSISTENT_STORAGE_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    help_text="Unieke naam voor dit opslagvolume binnen het component.",
)

PERSISTENT_STORAGE_SIZE = EditableVisualizer(
    editable=PERSISTENT_STORAGE_SIZE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Grootte",
    help_text="De maximale grootte van het opslagvolume.",
)

PERSISTENT_STORAGE_MOUNT_PATH = EditableVisualizer(
    editable=PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Het pad in de container waar het volume wordt gemount (bijv. /data, /var/lib/app).",
)

PERSISTENT_STORAGE_SEQUENCE = EditableVisualizer(
    editable=PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Persistente opslag",
    help_text="Persistente opslagvolumes die in de container worden gemount",
    children=[PERSISTENT_STORAGE_NAME, PERSISTENT_STORAGE_SIZE, PERSISTENT_STORAGE_MOUNT_PATH],
)
