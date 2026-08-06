"""Editable definitions for the postgresql-database service (project-level config).

The one user-editable thing at the project layer is the list of extra schemas (RC-17):
``services/postgresql-database/config/schemas``, a sequence of ``{postfix, description,
marked-for-deletion}`` items. Paths are built with ``config_path`` and every editable
carries ``virtualize=SERVICE_VIRTUALIZE`` so project-level service config
does not collide with the service-selection list in the wizard state.

The ``scope`` field is not offered here: it is set via YAML/API for now (a shared vs
project placement change is not a routine wizard edit).
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import SchemaPostfixValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType


def _cp(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.POSTGRESQL_DATABASE, "config", *segments)


# --- per-schema item fields --------------------------------------------------

SCHEMA_POSTFIX_EDITABLE = Editable(
    yaml_path=_cp("schemas[*]", "postfix"),
    validator=SchemaPostfixValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

SCHEMA_DESCRIPTION_EDITABLE = Editable(
    yaml_path=_cp("schemas[*]", "description"),
    virtualize=SERVICE_VIRTUALIZE,
)

# A safety gate, not a delete: marking keeps the schema (and its data) in the database
# but stops the provisioner from managing it and exposing its variable (RC-17 section 6).
SCHEMA_MARKED_EDITABLE = Editable(
    yaml_path=_cp("schemas[*]", "marked-for-deletion"),
    converter=BooleanConverter(),
    virtualize=SERVICE_VIRTUALIZE,
)

SCHEMA_ITEM_CHILD_EDITABLES = [
    SCHEMA_POSTFIX_EDITABLE,
    SCHEMA_DESCRIPTION_EDITABLE,
    SCHEMA_MARKED_EDITABLE,
]

SCHEMAS_EDITABLE = Editable(
    yaml_path=_cp("schemas"),
    min_items=0,
    max_items=20,
    children=SCHEMA_ITEM_CHILD_EDITABLES,
    virtualize=SERVICE_VIRTUALIZE,
)

# Flat list of every editable this service contributes at the project layer.
POSTGRESQL_SCHEMAS_EDITABLES = [SCHEMAS_EDITABLE, *SCHEMA_ITEM_CHILD_EDITABLES]
