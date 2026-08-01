"""temp-storage service (component-level: ephemeral volumes mounted into a component).

Self-contained service package: behaviour here, editables in ``editables.py``, the
committed JSON-schema fragment beside them (``temp-storage.v1.0.json``). The config
shape (a list of mount specs) is shared with persistent-storage and so lives in
``catalog/shared/storage.py``.
"""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.shared.storage import StorageConfig
from opi.services.catalog.temp_storage.editables import TEMP_STORAGE_SEQUENCE_EDITABLE
from opi.services.services_enums import ServiceType


class TempStorageService(Service):
    service_type = ServiceType.TEMP_STORAGE
    config_model = StorageConfig
    config_schema_version = "1.0"
    config_component_order = 20

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Config is a LIST of {name, size, mount-path} entries (see persistent-storage).

    def config_model_for(self, layer: ConfigLayer):
        # Mount specs on the component, per-mount clone state on the deployment-component.
        from opi.services.catalog.shared.storage import StorageCloneState

        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return StorageCloneState
        return self.config_model

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        return [TEMP_STORAGE_SEQUENCE_EDITABLE]

    def config_component_visualizers(self):
        from opi.services.catalog.temp_storage.visualizers import TEMP_STORAGE_SEQUENCE

        return [TEMP_STORAGE_SEQUENCE]

    def config_component_layout(self):
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
