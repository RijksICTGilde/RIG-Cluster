"""Editable definitions for the metrics-scraper service (component-level port/path)."""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import PathValidator

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
