"""Services section editables: the service selection field.

Per-service config editables live in their own service package
(``opi.services.catalog.<service>.editables``); this module holds only the
platform-level service *selection* field, which belongs to no single service.
"""

from __future__ import annotations

from opi.forms.editables.converters import ServiceListConverter
from opi.forms.editables.editable import Editable

SERVICES_EDITABLE = Editable(
    yaml_path="services",
    converter=ServiceListConverter(preserve_catalog_data=True),
    values_provider="ServiceOptionsProvider",
)
