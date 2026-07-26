"""Visualizers for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.authorization_wall.editables import AUTH_WALL_BANNER_EDITABLE

AUTH_WALL_BANNER = EditableVisualizer(
    editable=AUTH_WALL_BANNER_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Welkomstbanner tekst",
)
