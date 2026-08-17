"""Visualizers for the metrics-scraper service (component-level port/path).

Co-located with the service; consumed by the component-form aggregation via
``config_component_visualizers()``.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.metrics_scraper.editables import METRICS_PATH_EDITABLE, METRICS_PORT_EDITABLE

METRICS_PORT = EditableVisualizer(
    editable=METRICS_PORT_EDITABLE,
    widget=WidgetType.TEXT,
    label="Metrics poort",
    description="De poort waarop Prometheus metrics worden geserveerd.",
)

METRICS_PATH = EditableVisualizer(
    editable=METRICS_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Metrics pad",
    description="Het pad waarop de Prometheus metrics beschikbaar zijn.",
)
