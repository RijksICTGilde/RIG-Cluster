"""Components section editables: component definition fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    CommandLineConverter,
    ContainerImageConverter,
    IntegerConverter,
    ServiceListConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    AllowedValuesValidator,
    CommandLineValidator,
    ComponentNameValidator,
    ContainerImageValidator,
    MemoryRangeValidator,
    MemoryRequestRangeValidator,
    MinMaxLengthValidator,
    PathValidator,
)

# A service contributes its component fields through config_editables(ConfigLayer.COMPONENT),
# gathered here in config_component_order, so the tail of COMPONENTS_SEQUENCE_EDITABLE is not
# a hand-synced list.
from opi.services.registry import component_service_editables

COMPONENT_NAME_EDITABLE = Editable(
    yaml_path="components[*]/name",
    validator=ComponentNameValidator(),
    required=True,
)

COMPONENT_DESCRIPTION_EDITABLE = Editable(
    yaml_path="components[*]/description",
    validator=MinMaxLengthValidator(max_length=500),
    remove_when_none=True,
)

# What an image means on a top-level component is not settled; see
# features/component-image-field-clarification.md.
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
    default="64Mi",
)

COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE = Editable(
    yaml_path="components[*]/resources/limits/memory",
    values_provider="MemoryOptionsProvider",
    validator=MemoryRangeValidator(min_mi=25),
    default="256Mi",
)

COMPONENT_SERVICES_EDITABLE = Editable(
    yaml_path="components[*]/services",
    converter=ServiceListConverter(),
    values_provider="FilteredServiceOptionsProvider",
)

#: The container's start command. Kubernetes replaces the image's ENTRYPOINT with it, so a
#: value here silently discards whatever start-up logic the image brought along, and a command
#: the image does not have gives a pod that never starts with an error pointing nowhere
#: ("exec: \"sh\": executable file not found in $PATH"). It stays out of the file when left
#: empty: the schema demands minItems 1, so an empty list would not validate.
#:
#: One line of text for the user, a list of arguments in the file; ``CommandLineConverter``
#: splits the line and keeps whatever sits between double quotes together.
COMPONENT_COMMAND_EDITABLE = Editable(
    yaml_path="components[*]/command",
    converter=CommandLineConverter(),
    validator=CommandLineValidator(),
    remove_when_none=True,
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

COMPONENTS_SEQUENCE_EDITABLE = Editable(
    yaml_path="components",
    min_items=1,
    children=[
        COMPONENT_NAME_EDITABLE,
        COMPONENT_DESCRIPTION_EDITABLE,
        COMPONENT_IMAGE_EDITABLE,
        COMPONENT_COMMAND_EDITABLE,
        COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE,
        COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
        COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE,
        COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
        COMPONENT_PORTS_INBOUND_EDITABLE,
        COMPONENT_PORTS_OUTBOUND_EDITABLE,
        COMPONENT_SERVICES_EDITABLE,
        COMPONENT_PATH_EDITABLE,
        # Per-service component fields, gathered from the registry in config_component_order.
        # The aliases and user-env-vars system services own those two plain component
        # properties and sort first.
        *component_service_editables(),
    ],
)
