"""Editable definitions for the keycloak service (project-level SSO config).

Includes the nested, hand-authored sequences (additional-clients, realm-roles) that
stay hand-written rather than derived from the config model (the hard 10%).
"""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import RealmRoleValidator, UrlValidator

KEYCLOAK_TEMPLATE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/template",
    values_provider="KeycloakTemplateOptionsProvider",
    default="sso-support",
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris[*]",
    validator=UrlValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE],
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_RESTRICT_ACCESS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/enabled",
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/realm-role",
    default="allowed-user",
    depends_on="services/keycloak/config/restrict-access/enabled",
    validator=RealmRoleValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/error-message",
    default="${accessDeniedNoPermission}",
    depends_on="services/keycloak/config/restrict-access/enabled",
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_CLIENT_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/name",
    required=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris[*]",
    validator=UrlValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris",
    min_items=1,
    children=[KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE],
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients",
    min_items=0,
    max_items=5,
    children=[KEYCLOAK_CLIENT_NAME_EDITABLE, KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE],
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_ROLE_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/name",
    required=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_ROLE_DESCRIPTION_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles[*]/description",
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_REALM_ROLES_EDITABLE = Editable(
    yaml_path="services/keycloak/config/realm-roles",
    min_items=0,
    max_items=10,
    children=[KEYCLOAK_ROLE_NAME_EDITABLE, KEYCLOAK_ROLE_DESCRIPTION_EDITABLE],
    virtualize=SERVICE_VIRTUALIZE,
)
