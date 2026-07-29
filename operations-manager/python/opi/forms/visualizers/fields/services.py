"""Services visualizer constants: the service selection field.

Per-service config visualizers live in their own service package
(``opi.services.catalog.<service>.visualizers``); this module holds only the
platform-level service *selection* visualizer, which belongs to no single service.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.services import SERVICES_EDITABLE
from opi.forms.visualizers.visualizer import EditableVisualizer

SERVICES = EditableVisualizer(
    editable=SERVICES_EDITABLE,
    widget=WidgetType.SERVICE_CARDS,
    label="Beschikbare Services",
    description="Selecteer de services die u wilt activeren voor uw project",
)
