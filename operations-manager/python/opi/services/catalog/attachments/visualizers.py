"""Visualizers for the attachments service (component-level "uses" sequence).

Co-located with the service; consumed by the component-form aggregation via
``config_component_visualizers()``.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.attachments.editables import (
    ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ATTACHMENT_USE_PATH_EDITABLE,
    ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    ATTACHMENT_USE_REFERENCE_EDITABLE,
    ATTACHMENT_USE_SEQUENCE_EDITABLE,
)

ATTACHMENT_USE_REFERENCE = EditableVisualizer(
    editable=ATTACHMENT_USE_REFERENCE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bijlage",
    help_text="Kies een geuploade bijlage uit de catalogus.",
)

ATTACHMENT_USE_PROVIDE_AS = EditableVisualizer(
    editable=ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Leveren als",
    help_text="Als bestand op een pad, of als waarde van een env-var (alleen tekstbestanden).",
    attributes={"data-rerender": "true"},
)

ATTACHMENT_USE_PATH = EditableVisualizer(
    editable=ATTACHMENT_USE_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Vereist bij 'Als bestand': het pad in de container (bijv. /etc/tls/keystore.p12).",
)

ATTACHMENT_USE_ENV_NAME = EditableVisualizer(
    editable=ATTACHMENT_USE_ENV_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Env-var naam",
    help_text="Vereist bij 'Als env-var': de naam van de omgevingsvariabele (bijv. CA_BUNDLE).",
)

ATTACHMENT_USE_SEQUENCE = EditableVisualizer(
    editable=ATTACHMENT_USE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Bijlagen",
    help_text="Koppel geuploade bijlagen aan dit component (als bestand of env-var)",
    children=[ATTACHMENT_USE_REFERENCE, ATTACHMENT_USE_PROVIDE_AS, ATTACHMENT_USE_PATH, ATTACHMENT_USE_ENV_NAME],
)
