"""Editable definitions for the keycloak service (project-level SSO config).

Includes the nested, hand-authored sequences (additional-clients, realm-roles) that
stay hand-written rather than derived from the config model (the hard 10%).
"""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import RealmRoleValidator, UrlValidator

#: GEEN default, en dat is de hele reparatie.
#:
#: Hier stond ``default="sso-support"``, terwijl het configmodel, het API-schema en
#: ``KeycloakManager.DEFAULT_CONFIG`` alle drie ``sso-only`` gebruiken. Een projectbestand
#: zonder ``template`` liet het scherm dus "SSO Rijk of een lokaal account" zien terwijl het
#: platform de realm uit "Alleen SSO Rijk" bouwde: het formulier verzon een waarde die
#: nergens anders gold, en opslaan schreef die verzinsels ook nog het bestand in.
#:
#: Een van de twee kanten laten winnen was niet de keuze: welke blauwdruk een project krijgt
#: is een beslissing van de projectbeheerder en niet iets om stilzwijgend voor hem in te
#: vullen. Zonder default toont het veld wat er in het bestand staat, en anders niets - en
#: ``required`` dwingt dan een keuze af in plaats van er een te verzinnen.
KEYCLOAK_TEMPLATE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/template",
    values_provider="KeycloakTemplateOptionsProvider",
    required=True,
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

KEYCLOAK_RESTRICT_ACCESS_CLIENT_ROLE_EDITABLE = Editable(
    yaml_path="services/keycloak/config/restrict-access/role",
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
