"""persistent-storage service (component-level: mount volumes into a component).

Self-contained service package: behaviour here, editables in ``editables.py``,
visualizers in ``visualizers.py``, committed schema fragment beside them. The config
shape (a list of mount specs) is shared with temp-storage and lives in
``catalog/shared/storage.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.persistent_storage.editables import PERSISTENT_STORAGE_SEQUENCE_EDITABLE
from opi.services.catalog.persistent_storage.variables import PersistentStorageVariables
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.catalog.shared.storage import DEFAULT_STORAGE_SIZE, StorageConfig
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.layout import LayoutChild
    from opi.forms.visualizers.visualizer import EditableVisualizer


class PersistentStorageService(BackupsPageMixin, Service):
    service_type = ServiceType.PERSISTENT_STORAGE
    definition = ServiceDefinition(
        name="Permanente opslag",
        description="Gegevens blijven bewaard tijdens de levenscyclus van de applicatie",
        help_template="persistent_storage/help.md",
        icon="server",
        color="bruin",
        backup_label="pvc",
        # The same as temp-storage: a starting value, not an estimate. A PVC can grow but not
        # shrink, so starting too large costs more than starting too small.
        storage_config={"name": "data", "type": "persistent", "size": DEFAULT_STORAGE_SIZE, "mount-path": "/data"},
        variables=[var.value for var in PersistentStorageVariables],
        cleanup_strategy=CleanupStrategy.DEFERRED,
    )
    cleanup_manager_key = ManagerKey.PVC
    config_model = StorageConfig
    config_schema_version = "1.0"
    # May enrol itself: the mounts live on the component; the project layer holds
    # no storage decision.
    allows_implicit_project_selection = True
    config_component_order = 10

    # Component-level service: hooks a storage-mounts sequence into the component form.
    # Its config is a LIST of {name, size, mount-path} entries (StorageConfig is a
    # RootModel), so config_api_fields is not a flat field set -- left default.

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # Mount specs on the component, per-mount clone state on the deployment-component.
        from opi.services.catalog.shared.storage import StorageCloneState

        if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            return StorageCloneState
        return self.config_model

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        if layer is not ConfigLayer.COMPONENT:
            return []
        return [PERSISTENT_STORAGE_SEQUENCE_EDITABLE]

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        from opi.services.catalog.persistent_storage.visualizers import PERSISTENT_STORAGE_SEQUENCE

        return [PERSISTENT_STORAGE_SEQUENCE]

    def config_component_layout(self) -> list[LayoutChild]:
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]
