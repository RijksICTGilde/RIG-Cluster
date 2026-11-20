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


# Deployment-component layer: per-volume size override.
# Override and clone state sit in the same record, keyed by volume's render name.
DEPLOYMENT_COMP_PERSISTENT_STORAGE_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/persistent-storage[*]/reference",
    validator=KubernetesNameValidator("Volumenaam"),
    required=True,
)

DEPLOYMENT_COMP_PERSISTENT_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/persistent-storage[*]/config/size",
    values_provider="StorageSizeOverrideOptionsProvider",
    validator=StorageSizeValidator(),
    # No default: empty means "follow the component", and a default would write an override
    # the moment a deployment form is saved.
    remove_when_none=True,
)

DEPLOYMENT_COMP_PERSISTENT_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/persistent-storage",
    min_items=0,
    remove_when_none=True,
    children=[
        DEPLOYMENT_COMP_PERSISTENT_STORAGE_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_PERSISTENT_STORAGE_SIZE_EDITABLE,
    ],
)
