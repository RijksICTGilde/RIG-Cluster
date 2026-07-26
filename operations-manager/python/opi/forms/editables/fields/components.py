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

# The component form's service-specific fields are contributed by each service via
# config_editables(ConfigLayer.COMPONENT) and gathered here in config_component_order,
# so the tail of COMPONENTS_SEQUENCE_EDITABLE is not a hand-synced list. (The service
# editable definitions still authored below move into their service packages one
# service at a time; temp-storage already lives in catalog/temp_storage/editables.py.)
from opi.services.registry import component_service_editables

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
        # Per-service component fields, gathered from the registry in config_component_order
        # (persistent-storage, temp-storage, attachments, publish-on-web, metrics-scraper).
        *component_service_editables(),
    ],
)
