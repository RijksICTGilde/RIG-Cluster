"""Config generated visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.config_generated import (
    AGE_PRIVATE_KEY_GEN_EDITABLE,
    AGE_PUBLIC_KEY_GEN_EDITABLE,
    API_KEY_GEN_EDITABLE,
    PROJECT_NAME_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

GENERATED_EDITABLES: list[EditableVisualizer] = [
    EditableVisualizer(
        editable=PROJECT_NAME_EDITABLE,
        widget=WidgetType.HIDDEN,
        label="Projectnaam (technisch)",
    ),
    EditableVisualizer(
        editable=AGE_PUBLIC_KEY_GEN_EDITABLE,
        widget=WidgetType.HIDDEN,
        label="AGE publieke sleutel",
    ),
    EditableVisualizer(
        editable=AGE_PRIVATE_KEY_GEN_EDITABLE,
        widget=WidgetType.HIDDEN,
        label="AGE prive-sleutel (versleuteld)",
    ),
    EditableVisualizer(
        editable=API_KEY_GEN_EDITABLE,
        widget=WidgetType.HIDDEN,
        label="API-sleutel (versleuteld)",
    ),
]
