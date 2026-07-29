"""Editable definitions for the temp-storage service (component-level storage mounts)."""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import KubernetesNameValidator, PathValidator

TEMP_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/name",
    validator=KubernetesNameValidator("Opslagnaam"),
    required=True,
    default="tmp",
)

TEMP_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
    default="100Mi",
)

TEMP_STORAGE_MOUNT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/mount-path",
    validator=PathValidator(),
    required=True,
    default="/tmp",
)

TEMP_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config",
    depends_on="components[*]/services",
    show_when={"contains": "temp-storage"},
    virtualize=("services", "_services-config"),
    min_items=1,
    children=[
        TEMP_STORAGE_NAME_EDITABLE,
        TEMP_STORAGE_SIZE_EDITABLE,
        TEMP_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)
