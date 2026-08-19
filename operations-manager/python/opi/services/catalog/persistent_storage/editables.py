"""Editable definitions for the persistent-storage service (component-level storage mounts)."""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import KubernetesNameValidator, PathValidator, StorageSizeValidator
from opi.services.catalog.shared.storage import DEFAULT_STORAGE_SIZE

PERSISTENT_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
    validator=KubernetesNameValidator("Opslagnaam"),
    required=True,
    default="data",
)

PERSISTENT_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
    validator=StorageSizeValidator(),
    default=DEFAULT_STORAGE_SIZE,
)

PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/mount-path",
    validator=PathValidator(),
    required=True,
    default="/data",
)

PERSISTENT_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config",
    depends_on="components[*]/services",
    show_when={"contains": "persistent-storage"},
    virtualize=SERVICE_VIRTUALIZE,
    min_items=1,
    children=[
        PERSISTENT_STORAGE_NAME_EDITABLE,
        PERSISTENT_STORAGE_SIZE_EDITABLE,
        PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)
