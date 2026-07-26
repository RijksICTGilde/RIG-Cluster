"""persistent-storage service (component-level: mount volumes into a component).

Self-contained service package: behaviour here, editables in ``editables.py``,
visualizers in ``visualizers.py``, committed schema fragment beside them. The config
shape (a list of mount specs) is shared with temp-storage and lives in
``catalog/shared/storage.py``.
"""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.persistent_storage.editables import PERSISTENT_STORAGE_SEQUENCE_EDITABLE
from opi.services.catalog.shared.storage import StorageConfig
from opi.services.services_enums import ServiceType


class PersistentStorageService(Service):
    service_type = ServiceType.PERSISTENT_STORAGE
    cleanup_manager_key = "pvc"
    config_model = StorageConfig
    config_schema_version = "1.0"
    config_component_order = 10

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Its config is a LIST of {name, size, mount-path} entries (StorageConfig is a
    # RootModel), so config_api_fields is not a flat field set -- left default.

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        return [PERSISTENT_STORAGE_SEQUENCE_EDITABLE]

    def config_component_visualizers(self):
        from opi.services.catalog.persistent_storage.visualizers import PERSISTENT_STORAGE_SEQUENCE

        return [PERSISTENT_STORAGE_SEQUENCE]

    def config_component_layout(self):
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
