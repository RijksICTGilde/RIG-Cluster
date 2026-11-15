"""temp-storage service (component-level: ephemeral volumes mounted into a component).

Self-contained service package: behaviour here, editables in ``editables.py``, the
committed JSON-schema fragment beside them (``temp-storage.v1.0.json``). The config
shape (a list of mount specs) is shared with persistent-storage and so lives in
``catalog/shared/storage.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.shared.storage import DEFAULT_STORAGE_SIZE, StorageConfig
from opi.services.catalog.temp_storage.editables import TEMP_STORAGE_SEQUENCE_EDITABLE
from opi.services.catalog.temp_storage.variables import TempStorageVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.layout import LayoutChild
    from opi.forms.visualizers.visualizer import EditableVisualizer


class TempStorageService(Service):
    service_type = ServiceType.TEMP_STORAGE
    definition = ServiceDefinition(
        name="Tijdelijke schijfruimte",
        description="Gegevens worden niet bewaard tijdens de levenscyclus van de applicatie",
        help_template="temp_storage/help.md",
        icon="klok",
        color="oranje",
        # The smallest size that means anything in real use, and a value the form's option
        # list carries (StorageSizeOptionsProvider). Whoever needs more adds it; a starting
        # value should not be larger than what most components really use. The number lives in
        # DEFAULT_STORAGE_SIZE, because the form and the manifest generation lean on the same
        # starting size.
        storage_config={"name": "temp", "type": "ephemeral", "size": DEFAULT_STORAGE_SIZE, "mount-path": "/tmp"},
        variables=[var.value for var in TempStorageVariables],
    )
    config_model = StorageConfig
    config_schema_version = "1.0"
    # May enrol itself: the mounts live on the component; the project layer holds
    # no storage decision.
    allows_implicit_project_selection = True
    config_component_order = 20

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Config is a LIST of {name, size, mount-path} entries (see persistent-storage).

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # Mount specs on the component, per-mount clone state on the deployment-component.
        from opi.services.catalog.shared.storage import StorageCloneState

        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return StorageCloneState
        return self.config_model

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        if layer is not ConfigLayer.COMPONENT:
            return []
        return [TEMP_STORAGE_SEQUENCE_EDITABLE]

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        from opi.services.catalog.temp_storage.visualizers import TEMP_STORAGE_SEQUENCE

        return [TEMP_STORAGE_SEQUENCE]

    def config_component_layout(self) -> list[LayoutChild]:
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
