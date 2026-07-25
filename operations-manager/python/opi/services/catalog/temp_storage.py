"""temp-storage service (component-level: ephemeral volumes mounted into a component)."""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.config_models.storage import StorageConfig
from opi.services.services_enums import ServiceType


class TempStorageService(Service):
    service_type = ServiceType.TEMP_STORAGE
    config_model = StorageConfig
    config_schema_version = "1.0"

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Config is a LIST of {name, size, mount-path} entries (see persistent-storage).

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.forms.editables.fields.components import TEMP_STORAGE_SEQUENCE_EDITABLE

        return [TEMP_STORAGE_SEQUENCE_EDITABLE]

    def config_component_layout(self):
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
