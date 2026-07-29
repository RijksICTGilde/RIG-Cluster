"""Visualizers for the health-check service (component-level probe config).

Co-located with the service; consumed by the component-form aggregation via
``config_component_visualizers()``.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.health_check.editables import (
    HEALTH_CHECK_LIVENESS_PATH_EDITABLE,
    HEALTH_CHECK_PORT_EDITABLE,
    HEALTH_CHECK_READINESS_PATH_EDITABLE,
    HEALTH_CHECK_SCHEME_EDITABLE,
)

HEALTH_CHECK_SCHEME = EditableVisualizer(
    editable=HEALTH_CHECK_SCHEME_EDITABLE,
    widget=WidgetType.SELECT,
    label="Probe type",
    description=(
        "Hoe Kubernetes het component controleert. tcp opent alleen de socket; http/https "
        "doen een httpGet op de paden hieronder; none zet alle probes uit. Leeg laten valt "
        "terug op tcp op de eerste inbound-poort."
    ),
)

HEALTH_CHECK_PORT = EditableVisualizer(
    editable=HEALTH_CHECK_PORT_EDITABLE,
    widget=WidgetType.TEXT,
    label="Probe poort",
    description="De poort waarop geprobed wordt. Leeg laten gebruikt de eerste inbound-poort.",
)

HEALTH_CHECK_LIVENESS_PATH = EditableVisualizer(
    editable=HEALTH_CHECK_LIVENESS_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Liveness pad",
    description="Pad voor de liveness- en startup-probe (alleen bij http/https). Standaard /.",
)

HEALTH_CHECK_READINESS_PATH = EditableVisualizer(
    editable=HEALTH_CHECK_READINESS_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Readiness pad",
    description="Pad voor de readiness-probe (alleen bij http/https). Standaard /.",
)
