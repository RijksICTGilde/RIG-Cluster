"""Visualizers owned by the ``aliases`` system service (RC-25)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.aliases.editables import COMPONENT_ALIASES_EDITABLE

COMPONENT_ALIASES = EditableVisualizer(
    editable=COMPONENT_ALIASES_EDITABLE,
    widget=WidgetType.KEY_VALUE,
    label="Aliassen",
    description="Koppel platform-variabelen aan aangepaste namen voor dit component.",
    help_text=(
        "Wijs omgevingsvariabelen toe aan andere namen. "
        "Gebruik $VARIABELE_NAAM om platform-variabelen te refereren. "
        "Bijvoorbeeld: POSTGRES_HOST=$DATABASE_SERVER_HOST"
    ),
    attributes={"kv_format": "env"},
)
