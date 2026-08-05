"""Visualizers for the redis service (project-level ACL setting) -- RC-25."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.redis.editables import REDIS_ACL_KEY_PREFIX_EDITABLE

REDIS_ACL_KEY_PREFIX = EditableVisualizer(
    editable=REDIS_ACL_KEY_PREFIX_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Sleutels beperken tot dit project",
    description="Elke deployment krijgt een eigen Redis-gebruiker.",
    help_text=(
        "Aan (aanbevolen): de gebruiker mag alleen sleutels met het voorvoegsel "
        "{deployment}-{project}: gebruiken. Uit: de gebruiker kan bij elke sleutel in de "
        "gedeelde Redis. Zet dit alleen uit voor applicaties die hun sleutels niet kunnen "
        "voorvoegen."
    ),
)
