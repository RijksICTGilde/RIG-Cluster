"""Editable definitions for the temp-storage service (component-level storage mounts)."""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import KubernetesNameValidator, PathValidator, StorageSizeValidator
from opi.services.catalog.shared.storage import DEFAULT_STORAGE_SIZE

TEMP_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/name",
    validator=KubernetesNameValidator("Opslagnaam"),
    required=True,
    default="tmp",
)

TEMP_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
    validator=StorageSizeValidator(),
    default=DEFAULT_STORAGE_SIZE,
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
    virtualize=SERVICE_VIRTUALIZE,
    min_items=1,
    children=[
        TEMP_STORAGE_NAME_EDITABLE,
        TEMP_STORAGE_SIZE_EDITABLE,
        TEMP_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)


# The deployment-component layer: this deployment's own size per volume. Same shape as
# persistent-storage, under its own service key; see that package for the reasoning.
DEPLOYMENT_COMP_TEMP_STORAGE_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/temp-storage[*]/reference",
    validator=KubernetesNameValidator("Volumenaam"),
    required=True,
)

DEPLOYMENT_COMP_TEMP_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/temp-storage[*]/config/size",
    values_provider="StorageSizeOverrideOptionsProvider",
    validator=StorageSizeValidator(),
    remove_when_none=True,
)

DEPLOYMENT_COMP_TEMP_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/temp-storage",
    min_items=0,
    remove_when_none=True,
    children=[
        DEPLOYMENT_COMP_TEMP_STORAGE_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_TEMP_STORAGE_SIZE_EDITABLE,
    ],
)
