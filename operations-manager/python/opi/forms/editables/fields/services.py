"""Services section editables: service selection and per-service config fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    EmptyToNoneConverter,
    ServiceListConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import RangeValidator, RealmRoleValidator, UrlValidator

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

SERVICES_EDITABLE = Editable(
    yaml_path="services",
    converter=ServiceListConverter(),
    values_provider="ServiceOptionsProvider",
)

# --- Keycloak ---

KEYCLOAK_TEMPLATE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/template",
    values_provider="KeycloakTemplateOptionsProvider",
)

KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris[*]",
    validator=UrlValidator(),
)

KEYCLOAK_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE],
)

KEYCLOAK_RESTRICT_ACCESS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/enabled",
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
)

KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/realm-role",
    default="allowed-user",
    depends_on="services/keycloak/config/restrict-access/enabled",
    validator=RealmRoleValidator(),
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/error-message",
    default="${accessDeniedNoPermission}",
    depends_on="services/keycloak/config/restrict-access/enabled",
)

KEYCLOAK_CLIENT_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/name",
    required=True,
)

KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris[*]",
    validator=UrlValidator(),
)

KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris",
    min_items=1,
    children=[KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE],
)

KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients",
    min_items=0,
    max_items=5,
    children=[KEYCLOAK_CLIENT_NAME_EDITABLE, KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE],
)

KEYCLOAK_ROLE_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/name",
    required=True,
)

KEYCLOAK_ROLE_DESCRIPTION_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/description",
)

KEYCLOAK_REALM_ROLES_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_ROLE_NAME_EDITABLE, KEYCLOAK_ROLE_DESCRIPTION_EDITABLE],
)

# --- PostgreSQL ---

POSTGRESQL_INSTANCES_EDITABLE = Editable(
    yaml_path="services/namespace-postgresql-database/config/instances",
    validator=RangeValidator(min_value=1, max_value=5),
)

POSTGRESQL_STORAGE_EDITABLE = Editable(
    yaml_path="services/namespace-postgresql-database/config/storage",
    values_provider="StorageSizeOptionsProvider",
)

# --- Authorization wall ---

AUTH_WALL_BANNER_EDITABLE = Editable(
    yaml_path="services/authorization-wall/config/banner",
)
