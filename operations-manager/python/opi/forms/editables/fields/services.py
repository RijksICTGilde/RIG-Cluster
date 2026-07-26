"""Services section editables: service selection and per-service config fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    EmptyToNoneConverter,
    ServiceListConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import RangeValidator, RealmRoleValidator, UrlValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

SERVICES_EDITABLE = Editable(
    yaml_path="services",
    converter=ServiceListConverter(preserve_catalog_data=True),
    values_provider="ServiceOptionsProvider",
)

# --- Keycloak ---

_SVC_VIRT = ("services", "_services-config")
"""Virtualize mapping for project-level service config editables.

Uses the same pattern as component-level service configs (persistent-storage,
temp-storage, metrics-scraper) to avoid collisions between the service
selection list and per-service config in wizard step_data.
"""

KEYCLOAK_TEMPLATE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/template",
    values_provider="KeycloakTemplateOptionsProvider",
    default="sso-support",
    virtualize=_SVC_VIRT,
)

KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris[*]",
    validator=UrlValidator(),
    virtualize=_SVC_VIRT,
)

KEYCLOAK_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE],
    virtualize=_SVC_VIRT,
)

KEYCLOAK_RESTRICT_ACCESS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/enabled",
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=_SVC_VIRT,
)

KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/realm-role",
    default="allowed-user",
    depends_on="services/keycloak/config/restrict-access/enabled",
    validator=RealmRoleValidator(),
    virtualize=_SVC_VIRT,
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/error-message",
    default="${accessDeniedNoPermission}",
    depends_on="services/keycloak/config/restrict-access/enabled",
    virtualize=_SVC_VIRT,
)

KEYCLOAK_CLIENT_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/name",
    required=True,
    virtualize=_SVC_VIRT,
)

KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris[*]",
    validator=UrlValidator(),
    virtualize=_SVC_VIRT,
)

KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris",
    min_items=1,
    children=[KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE],
    virtualize=_SVC_VIRT,
)

KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients",
    min_items=0,
    max_items=5,
    children=[KEYCLOAK_CLIENT_NAME_EDITABLE, KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE],
    virtualize=_SVC_VIRT,
)

KEYCLOAK_ROLE_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/name",
    required=True,
    virtualize=_SVC_VIRT,
)

KEYCLOAK_ROLE_DESCRIPTION_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/description",
    virtualize=_SVC_VIRT,
)

KEYCLOAK_REALM_ROLES_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_ROLE_NAME_EDITABLE, KEYCLOAK_ROLE_DESCRIPTION_EDITABLE],
    virtualize=_SVC_VIRT,
)

# --- PostgreSQL ---

POSTGRESQL_INSTANCES_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, "config", "instances"),
    validator=RangeValidator(min_value=1, max_value=5),
    virtualize=_SVC_VIRT,
)

POSTGRESQL_STORAGE_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.NAMESPACE_POSTGRESQL_DATABASE, "config", "storage"),
    values_provider="StorageSizeOptionsProvider",
    virtualize=_SVC_VIRT,
)
