"""Components section editables: component definition fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    ContainerImageConverter,
    IntegerConverter,
    KeyValueConverter,
    ServiceListConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    AllowedValuesValidator,
    ComponentNameValidator,
    ContainerImageValidator,
    KeyValueValidator,
    PathValidator,
)

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

COMPONENT_NAME_EDITABLE = Editable(
    yaml_path="components[*]/name",
    validator=ComponentNameValidator(),
    required=True,
)

COMPONENT_IMAGE_EDITABLE = Editable(
    yaml_path="components[*]/image",
    validator=ContainerImageValidator(),
    converter=ContainerImageConverter(),
)

INBOUND_PORT_EDITABLE = Editable(
    yaml_path="components[*]/ports/inbound[*]",
    converter=IntegerConverter(),
)

COMPONENT_PORTS_INBOUND_EDITABLE = Editable(
    yaml_path="components[*]/ports/inbound",
    default=[8080],
    children=[INBOUND_PORT_EDITABLE],
)

OUTBOUND_PORT_EDITABLE = Editable(
    yaml_path="components[*]/ports/outbound[*]",
    converter=IntegerConverter(),
)

COMPONENT_PORTS_OUTBOUND_EDITABLE = Editable(
    yaml_path="components[*]/ports/outbound",
    default=[80, 443],
    children=[OUTBOUND_PORT_EDITABLE],
)

COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE = Editable(
    yaml_path="components[*]/resources/cpu/request",
    values_provider="CpuRequestOptionsProvider",
    validator=AllowedValuesValidator(["50m", "100m", "250m", "500m"]),
    default="50m",
)

COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE = Editable(
    yaml_path="components[*]/resources/cpu/limit",
    values_provider="CpuLimitOptionsProvider",
    validator=AllowedValuesValidator(["500m", "1"]),
    default="1",
)

COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE = Editable(
    yaml_path="components[*]/resources/memory/request",
    values_provider="MemoryRequestOptionsProvider",
    validator=AllowedValuesValidator(["256Mi", "512Mi"]),
    default="256Mi",
)

COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE = Editable(
    yaml_path="components[*]/resources/memory/limit",
    values_provider="MemoryLimitOptionsProvider",
    validator=AllowedValuesValidator(["512Mi", "768Mi", "1Gi"]),
    default="512Mi",
)

COMPONENT_SERVICES_EDITABLE = Editable(
    yaml_path="components[*]/services",
    converter=ServiceListConverter(),
    values_provider="FilteredServiceOptionsProvider",
    default="__all__",
)

COMPONENT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/path",
    default="/",
    validator=PathValidator(),
)

COMPONENT_REWRITE_PATH_EDITABLE = Editable(
    yaml_path="components[*]/rewrite-path",
    validator=PathValidator(),
)

COMPONENT_ALIASES_EDITABLE = Editable(
    yaml_path="components[*]/aliases",
    converter=KeyValueConverter(fmt="env"),
    validator=KeyValueValidator(),
)

COMPONENT_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="components[*]/user-env-vars",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=KeyValueValidator(),
)

PERSISTENT_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
    required=True,
)

PERSISTENT_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
)

PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/mount-path",
    required=True,
)

PERSISTENT_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config",
    depends_on="services",
    show_when={"contains": "persistent-storage"},
    children=[
        PERSISTENT_STORAGE_NAME_EDITABLE,
        PERSISTENT_STORAGE_SIZE_EDITABLE,
        PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)

TEMP_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/name",
    required=True,
)

TEMP_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
)

TEMP_STORAGE_MOUNT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/mount-path",
    required=True,
)

TEMP_STORAGE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config",
    depends_on="services",
    show_when={"contains": "temp-storage"},
    children=[
        TEMP_STORAGE_NAME_EDITABLE,
        TEMP_STORAGE_SIZE_EDITABLE,
        TEMP_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)

COMPONENTS_SEQUENCE_EDITABLE = Editable(
    yaml_path="components",
    min_items=1,
    children=[
        COMPONENT_NAME_EDITABLE,
        COMPONENT_IMAGE_EDITABLE,
        COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE,
        COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
        COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE,
        COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
        COMPONENT_PORTS_INBOUND_EDITABLE,
        COMPONENT_PORTS_OUTBOUND_EDITABLE,
        COMPONENT_SERVICES_EDITABLE,
        COMPONENT_PATH_EDITABLE,
        COMPONENT_REWRITE_PATH_EDITABLE,
        COMPONENT_ALIASES_EDITABLE,
        COMPONENT_USER_ENV_VARS_EDITABLE,
        PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
        TEMP_STORAGE_SEQUENCE_EDITABLE,
    ],
)
