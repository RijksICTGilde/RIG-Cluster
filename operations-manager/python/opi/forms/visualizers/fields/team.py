"""Team visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.team import (
    USER_EMAIL_EDITABLE,
    USER_ROLE_EDITABLE,
    USERS_SEQUENCE_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

USER_EMAIL = EditableVisualizer(
    editable=USER_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="E-mailadres",
)

USER_ROLE = EditableVisualizer(
    editable=USER_ROLE_EDITABLE,
    widget=WidgetType.HIDDEN,
    label="Rol",
)

USERS_SEQUENCE = EditableVisualizer(
    editable=USERS_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Projectleden",
    children=[USER_EMAIL, USER_ROLE],
)
