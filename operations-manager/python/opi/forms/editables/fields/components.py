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
    MemoryRangeValidator,
    MemoryRequestRangeValidator,
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

# TODO: clarify purpose of image on top-level components — see features/component-image-field-clarification.md
COMPONENT_IMAGE_EDITABLE = Editable(
    yaml_path="components[*]/image",
    validator=ContainerImageValidator(),
    converter=ContainerImageConverter(),
    remove_when_none=True,
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
    yaml_path="components[*]/resources/requests/cpu",
    values_provider="CpuRequestOptionsProvider",
    validator=AllowedValuesValidator(["50m", "100m", "250m", "500m"]),
    default="50m",
)

COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE = Editable(
    yaml_path="components[*]/resources/limits/cpu",
    values_provider="CpuLimitOptionsProvider",
    validator=AllowedValuesValidator(["500m", "1"]),
    default="1",
)

COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE = Editable(
    yaml_path="components[*]/resources/requests/memory",
    values_provider="MemoryRequestOptionsProvider",
    validator=MemoryRequestRangeValidator(min_mi=25),
    default="256Mi",
)

COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE = Editable(
    yaml_path="components[*]/resources/limits/memory",
    values_provider="MemoryOptionsProvider",
    validator=MemoryRangeValidator(min_mi=25),
    default="512Mi",
)

COMPONENT_SERVICES_EDITABLE = Editable(
    yaml_path="components[*]/services",
    converter=ServiceListConverter(),
    values_provider="FilteredServiceOptionsProvider",
)

COMPONENT_PATH_MATCH_EDITABLE = Editable(
    yaml_path="components[*]/path[*]/match",
    default="/",
    validator=PathValidator(),
    required=True,
)

COMPONENT_PATH_REWRITE_EDITABLE = Editable(
    yaml_path="components[*]/path[*]/rewrite",
    validator=PathValidator(),
    remove_when_none=True,
)

COMPONENT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/path",
    default=[{"match": "/"}],
    min_items=1,
    children=[
        COMPONENT_PATH_MATCH_EDITABLE,
        COMPONENT_PATH_REWRITE_EDITABLE,
    ],
)

COMPONENT_ALIASES_EDITABLE = Editable(
    yaml_path="components[*]/aliases",
    converter=KeyValueConverter(fmt="env"),
    validator=KeyValueValidator(),
    remove_when_none=True,
)

COMPONENT_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="components[*]/user-env-vars",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=KeyValueValidator(),
    remove_when_none=True,
)

PERSISTENT_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
    required=True,
    default="data",
)

PERSISTENT_STORAGE_SIZE_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/size",
    values_provider="StorageSizeOptionsProvider",
    default="100Mi",
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
    virtualize=("services", "_services-config"),
    min_items=1,
    children=[
        PERSISTENT_STORAGE_NAME_EDITABLE,
        PERSISTENT_STORAGE_SIZE_EDITABLE,
        PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    ],
)

TEMP_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/name",
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

ATTACHMENT_USE_REFERENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/reference",
    values_provider="AttachmentOptionsProvider",
    required=True,
)

ATTACHMENT_USE_PROVIDE_AS_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/provide-as",
    values_provider="AttachmentProvideAsOptionsProvider",
    required=True,
    default="file",
)

ATTACHMENT_USE_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="components[*]/services{attachments}/config[*]/provide-as",
    show_when={"value": ["file"]},
)

ATTACHMENT_USE_ENV_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/env-name",
    remove_when_none=True,
    depends_on="components[*]/services{attachments}/config[*]/provide-as",
    show_when={"value": ["env-var"]},
)

ATTACHMENT_USE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config",
    depends_on="components[*]/services",
    show_when={"contains": "attachments"},
    virtualize=("services", "_services-config"),
    min_items=0,
    remove_when_none=True,
    children=[
        ATTACHMENT_USE_REFERENCE_EDITABLE,
        ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
        ATTACHMENT_USE_PATH_EDITABLE,
        ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ],
)

PUBLISH_ON_WEB_TLS_EDITABLE = Editable(
    yaml_path="components[*]/services{publish-on-web}/config/tls",
    values_provider="PublishTlsModeOptionsProvider",
    default="standard",
    virtualize=("services", "_services-config"),
    depends_on="components[*]/services",
    show_when={"contains": "publish-on-web"},
)

METRICS_PORT_EDITABLE = Editable(
    yaml_path="components[*]/services{metrics-scraper}/port",
    converter=IntegerConverter(),
    required=True,
    default=8080,
    depends_on="components[*]/services",
    show_when={"contains": "metrics-scraper"},
    virtualize=("services", "_services-config"),
)

METRICS_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{metrics-scraper}/path",
    default="/metrics",
    validator=PathValidator(),
    required=True,
    depends_on="components[*]/services",
    show_when={"contains": "metrics-scraper"},
    virtualize=("services", "_services-config"),
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
        COMPONENT_ALIASES_EDITABLE,
        COMPONENT_USER_ENV_VARS_EDITABLE,
        PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
        TEMP_STORAGE_SEQUENCE_EDITABLE,
        ATTACHMENT_USE_SEQUENCE_EDITABLE,
        PUBLISH_ON_WEB_TLS_EDITABLE,
        METRICS_PORT_EDITABLE,
        METRICS_PATH_EDITABLE,
    ],
)
