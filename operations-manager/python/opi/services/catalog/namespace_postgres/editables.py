"""Editable definitions for the namespace-postgresql-database service (project-level instances + storage)."""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import RangeValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

POSTGRESQL_INSTANCES_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, "config", "instances"),
    validator=RangeValidator(min_value=1, max_value=5),
    virtualize=("services", "_services-config"),
)

POSTGRESQL_STORAGE_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, "config", "storage"),
    values_provider="StorageSizeOptionsProvider",
    virtualize=("services", "_services-config"),
)
