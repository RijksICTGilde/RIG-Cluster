"""Team visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.team import (
    USER_EMAIL_EDITABLE,
    USER_ROLES_EDITABLE,
    USERS_SEQUENCE_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

USER_EMAIL = EditableVisualizer(
    editable=USER_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="E-mailadres",
)

USER_ROLES = EditableVisualizer(
    editable=USER_ROLES_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Rollen",
    description="Wat dit lid in dit project mag. Een lid kan meer dan een rol hebben.",
)

USERS_SEQUENCE = EditableVisualizer(
    editable=USERS_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Projectleden",
    children=[USER_EMAIL, USER_ROLES],
)
