"""Visualizers owned by the ``user-env-vars`` system service (RC-25)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.user_env_vars.editables import (
    COMPONENT_USER_ENV_VARS_EDITABLE,
    DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE,
)

COMPONENT_USER_ENV_VARS = EditableVisualizer(
    editable=COMPONENT_USER_ENV_VARS_EDITABLE,
    widget=WidgetType.KEY_VALUE,
    label="Eigen omgevingsvariabelen",
    description="Voeg eigen omgevingsvariabelen toe voor dit component.",
    help_text=(
        "Definieer extra omgevingsvariabelen die beschikbaar worden in de container. "
        "Deze waarden worden versleuteld opgeslagen. "
        "Bijvoorbeeld: API_KEY=mijn-geheime-sleutel"
    ),
    attributes={"kv_format": "env"},
)

DEPLOYMENT_COMP_USER_ENV_VARS = EditableVisualizer(
    editable=DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE,
    widget=WidgetType.KEY_VALUE,
    label="Omgevingsvariabelen",
    description="Deployment-specifieke omgevingsvariabelen voor dit component.",
    help_text=(
        "Overschrijft de omgevingsvariabelen uit de componentdefinitie voor deze deployment. "
        "Bijvoorbeeld: API_URL=https://api.production.example.com"
    ),
    attributes={"kv_format": "env"},
)
