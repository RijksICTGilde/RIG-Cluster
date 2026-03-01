"""Config display visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.config_display import (
    AGE_PRIVATE_KEY_EDITABLE,
    AGE_PUBLIC_KEY_EDITABLE,
    API_KEY_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

AGE_PUBLIC_KEY = EditableVisualizer(
    editable=AGE_PUBLIC_KEY_EDITABLE,
    widget=WidgetType.DISPLAY_CARD,
    label="AGE publieke sleutel",
    readonly=True,
)

AGE_PRIVATE_KEY = EditableVisualizer(
    editable=AGE_PRIVATE_KEY_EDITABLE,
    widget=WidgetType.DISPLAY_CARD,
    label="AGE privé sleutel",
    readonly=True,
)

API_KEY = EditableVisualizer(
    editable=API_KEY_EDITABLE,
    widget=WidgetType.DISPLAY_CARD,
    label="API sleutel",
    readonly=True,
)
