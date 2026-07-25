"""persistent-storage service."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.config_models.storage import StorageConfig
from opi.services.services_enums import ServiceType


class PersistentStorageService(Service):
    service_type = ServiceType.PERSISTENT_STORAGE
    cleanup_manager_key = "pvc"
    config_model = StorageConfig
    config_schema_version = "1.0"
