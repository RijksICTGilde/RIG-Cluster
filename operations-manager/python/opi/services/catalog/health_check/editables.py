"""Editable definitions for the health-check service (component-level probe config).

All fields are optional and carry NO default: seeding a default onto a component that
has not selected the service would materialise ``services{health-check}`` into the list
as a side effect (the ``{K}`` path filter), quietly turning a default into a selection.
The effective defaults live in generic code and the template, not here.
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import PathValidator, RangeValidator

HEALTH_CHECK_SCHEME_EDITABLE = Editable(
    yaml_path="components[*]/services{health-check}/config/scheme",
    values_provider="HealthCheckSchemeOptionsProvider",
    remove_when_none=True,
    depends_on="components[*]/services",
    show_when={"contains": "health-check"},
    virtualize=("services", "_services-config"),
)

HEALTH_CHECK_PORT_EDITABLE = Editable(
    yaml_path="components[*]/services{health-check}/config/port",
    converter=IntegerConverter(),
    validator=RangeValidator(min_value=1, max_value=65535),
    remove_when_none=True,
    depends_on="components[*]/services",
    show_when={"contains": "health-check"},
    virtualize=("services", "_services-config"),
)

HEALTH_CHECK_LIVENESS_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{health-check}/config/liveness-path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="components[*]/services",
    show_when={"contains": "health-check"},
    virtualize=("services", "_services-config"),
)

HEALTH_CHECK_READINESS_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{health-check}/config/readiness-path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="components[*]/services",
    show_when={"contains": "health-check"},
    virtualize=("services", "_services-config"),
)
