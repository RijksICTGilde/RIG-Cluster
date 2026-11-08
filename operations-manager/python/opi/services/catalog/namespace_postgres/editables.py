"""Editable definitions for the namespace-postgresql-database service (project-level instances + storage)."""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ConfigSettingValidator, RangeValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.shared.postgres_settings import POSTGRES_STORAGE
from opi.services.services_enums import ServiceType

POSTGRESQL_INSTANCES_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, "config", "instances"),
    validator=RangeValidator(min_value=1, max_value=5),
    virtualize=SERVICE_VIRTUALIZE,
)

POSTGRESQL_STORAGE_EDITABLE = Editable(
    yaml_path=POSTGRES_STORAGE.yaml_path(ServiceType.NAMESPACE_POSTGRESQL_DATABASE, ConfigLayer.PROJECT),
    # An option list is not a check: the bound lives in the declaration, which also judges a
    # hand-posted value.
    validator=ConfigSettingValidator(POSTGRES_STORAGE),
    values_provider="PostgresStorageSizeOptionsProvider",
    virtualize=SERVICE_VIRTUALIZE,
)
