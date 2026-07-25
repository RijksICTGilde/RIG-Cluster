"""persistent-storage service (component-level: mount volumes into a component)."""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.config_models.storage import StorageConfig
from opi.services.services_enums import ServiceType


class PersistentStorageService(Service):
    service_type = ServiceType.PERSISTENT_STORAGE
    cleanup_manager_key = "pvc"
    config_model = StorageConfig
    config_schema_version = "1.0"

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Its config is a LIST of {name, size, mount-path} entries (StorageConfig is a
    # RootModel), so config_api_fields is not a flat field set -- left default.

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.forms.editables.fields.components import PERSISTENT_STORAGE_SEQUENCE_EDITABLE

        return [PERSISTENT_STORAGE_SEQUENCE_EDITABLE]

    def config_component_layout(self):
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
