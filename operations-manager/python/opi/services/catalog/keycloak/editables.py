"""Editable definitions for the keycloak service (project-level SSO config).

Includes the nested, hand-authored sequences (additional-clients, realm-roles), which are
not derived from the config model.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.converters import EmptyToNoneConverter, LogoutPathConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.service_path import smart_get_value
from opi.forms.editables.validators import LogoutPathValidator, RealmRoleValidator, RedirectUriValidator

#: NO default, deliberately.
#:
#: A default here invents a value. The config model, the API schema and
#: ``KeycloakManager.DEFAULT_CONFIG`` all three use ``sso-only``, so a form default of its own
#: shows one blueprint on screen while the platform builds the realm from another -- and
#: writes that invention into the file on save. Which blueprint a project gets is a decision
#: for the project administrator, so the field shows what is in the file and otherwise nothing,
#: and ``required`` forces a choice instead of inventing one.
KEYCLOAK_TEMPLATE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/template",
    values_provider="KeycloakTemplateOptionsProvider",
    required=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional_redirect_uris[*]",
    validator=RedirectUriValidator(),
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

RESTRICT_ACCESS_CLIENT_ROLE_PATH = "services/keycloak/config/restrict-access/role"


def _default_realm_role(data: dict[str, Any]) -> str | None:
    """``allowed-user``, unless a client role is set: that one gates, so no realm role is invented."""
    return None if smart_get_value(data, RESTRICT_ACCESS_CLIENT_ROLE_PATH) else "allowed-user"


KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/realm-role",
    default=_default_realm_role,
    depends_on="services/keycloak/config/restrict-access/enabled",
    validator=RealmRoleValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_RESTRICT_ACCESS_CLIENT_ROLE_EDITABLE = Editable(
    yaml_path=RESTRICT_ACCESS_CLIENT_ROLE_PATH,
    depends_on="services/keycloak/config/restrict-access/enabled",
    validator=RealmRoleValidator(),
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_ACCOUNT_LINK_EDITABLE = Editable(
    yaml_path="services/keycloak/config/account-link",
    values_provider="KeycloakAccountLinkOptionsProvider",
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/error-message",
    default="${accessDeniedNoPermission}",
    depends_on="services/keycloak/config/restrict-access/enabled",
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_LOGOUT_PATH_EDITABLE = Editable(
    yaml_path="services/keycloak/config/logout-path",
    validator=LogoutPathValidator(),
    converter=LogoutPathConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_CLIENT_NAME_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/name",
    required=True,
    virtualize=SERVICE_VIRTUALIZE,
)

KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE = Editable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris[*]",
    validator=RedirectUriValidator(),
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
