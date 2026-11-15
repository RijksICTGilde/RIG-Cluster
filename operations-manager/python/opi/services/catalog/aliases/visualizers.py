"""Visualizers owned by the ``aliases`` system service."""

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
    # The help dialog lists the variables usable here, which the help text above does not
    # name.
    help_template="aliassen.html.j2",
    # kv_completions turns on completion after a dollar sign. The widget adapter reads that
    # list from the same source as the help dialog and the validation, so the editor can
    # never suggest a name the form rejects.
    attributes={"kv_format": "env", "kv_completions": "aliassen"},
)
