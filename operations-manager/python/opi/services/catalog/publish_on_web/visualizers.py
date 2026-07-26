"""Visualizers for the publish-on-web service (component-level TLS + attachment).

Co-located with the service; consumed by the component-form aggregation via
``config_component_visualizers()``.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.publish_on_web.editables import (
    PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    PUBLISH_ON_WEB_TLS_EDITABLE,
)

PUBLISH_ON_WEB_TLS = EditableVisualizer(
    editable=PUBLISH_ON_WEB_TLS_EDITABLE,
    widget=WidgetType.SELECT,
    label="TLS-modus",
    help_text=(
        "Standaard: het platform regelt het certificaat. Passthrough: de pod presenteert "
        "z'n eigen certificaat (koppel dat als bijlage aan dit component). Aangeleverd: eigen "
        "certificaat op de ingress (kies de PEM-bijlage). Let op: passthrough werkt alleen als "
        "dit component een eigen hostname heeft (een domain-format met het component erin), of "
        "als dit het enige gepubliceerde component is."
    ),
    attributes={"data-rerender": "true"},
)

PUBLISH_ON_WEB_ATTACHMENT = EditableVisualizer(
    editable=PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Certificaat (bijlage)",
    help_text="De PEM-bijlage (cert + key) die als certificaat op de ingress komt.",
)
