"""Services section editables: service selection and per-service config fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    ServiceListConverter,
)
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import RangeValidator, RealmRoleValidator, UrlValidator

# ---------------------------------------------------------------------------
# Top-level service selection
# ---------------------------------------------------------------------------

SERVICES = ProjectEditable(
    yaml_path="services",
    widget="service_cards",
    label="Beschikbare Services",
    description="Selecteer de services die u wilt activeren voor uw project",
    converter=ServiceListConverter(),
    options_provider="ServiceOptionsProvider",
)

# ---------------------------------------------------------------------------
# Keycloak config
# ---------------------------------------------------------------------------

KEYCLOAK_TEMPLATE = ProjectEditable(
    yaml_path="services/keycloak/config/template",
    widget="select",
    label="Keycloak template",
    options_provider="KeycloakTemplateOptionsProvider",
)

# --- Redirect URIs (sequence of individual entries) ---

KEYCLOAK_REDIRECT_URI_ITEM = ProjectEditable(
    yaml_path="services/keycloak/config/additional_redirect_uris[*]",
    widget="text",
    label="URI",
    validator=UrlValidator(),
)

KEYCLOAK_REDIRECT_URIS = ProjectEditable(
    yaml_path="services/keycloak/config/additional_redirect_uris",
    widget="sequence",
    label="Extra redirect URI's",
    children=[KEYCLOAK_REDIRECT_URI_ITEM],
    min_items=0,
    max_items=10,
    help_text="Redirect URI's voor lokale ontwikkeling of externe integraties.",
)

# --- Restrict access ---

KEYCLOAK_RESTRICT_ACCESS = ProjectEditable(
    yaml_path="services/keycloak/config/restrict-access/enabled",
    widget="checkbox",
    label="Toegang beperken",
    help_text="Wanneer ingeschakeld kunnen alleen gebruikers met de opgegeven realm-rol de applicatie openen",
    attributes={"data-rerender": "true"},
    locked_by_service="authorization-wall",
)

KEYCLOAK_RESTRICT_ACCESS_ROLE = ProjectEditable(
    yaml_path="services/keycloak/config/restrict-access/realm-role",
    widget="text",
    label="Realm rol",
    default="allowed-user",
    depends_on="services/keycloak/config/restrict-access/enabled",
    help_text=(
        "De naam van de Keycloak realm-rol die toegang verleent. "
        "Elke gebruiker met deze rol mag de applicatie gebruiken. "
        "De rol wordt door de projectbeheerder aan gebruikers toegekend; "
        "de exacte naam maakt niet uit. Bij twijfel: gebruik de standaardwaarde."
    ),
    validator=RealmRoleValidator(),
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG = ProjectEditable(
    yaml_path="services/keycloak/config/restrict-access/error-message",
    widget="text",
    label="Foutmelding",
    default="${accessDeniedNoPermission}",
    depends_on="services/keycloak/config/restrict-access/enabled",
    help_text=(
        "Bericht dat wordt getoond wanneer een gebruiker geen toegang heeft. "
        "De standaardwaarde gebruikt een vertaalbaar template. "
        "Bij twijfel: gebruik de standaardwaarde."
    ),
)

# --- Additional clients (nested sequence) ---

KEYCLOAK_CLIENT_NAME = ProjectEditable(
    yaml_path="services/keycloak/config/additional-clients[*]/name",
    widget="text",
    label="Client naam",
    required=True,
)

KEYCLOAK_CLIENT_REDIRECT_URI = ProjectEditable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris[*]",
    widget="text",
    label="URI",
    validator=UrlValidator(),
)

KEYCLOAK_CLIENT_REDIRECT_URIS = ProjectEditable(
    yaml_path="services/keycloak/config/additional-clients[*]/redirect-uris",
    widget="sequence",
    label="Redirect URI's",
    children=[KEYCLOAK_CLIENT_REDIRECT_URI],
    min_items=1,
)

KEYCLOAK_ADDITIONAL_CLIENTS = ProjectEditable(
    yaml_path="services/keycloak/config/additional-clients",
    widget="sequence",
    label="Extra Keycloak clients",
    children=[KEYCLOAK_CLIENT_NAME, KEYCLOAK_CLIENT_REDIRECT_URIS],
    min_items=0,
    max_items=5,
    help_text="Extra clients voor microservices of externe applicaties die dezelfde realm delen.",
)

# --- Realm roles ---

KEYCLOAK_ROLE_NAME = ProjectEditable(
    yaml_path="services/keycloak/config/realm-roles[*]/name",
    widget="text",
    label="Rolnaam",
    required=True,
)

KEYCLOAK_ROLE_DESCRIPTION = ProjectEditable(
    yaml_path="services/keycloak/config/realm-roles[*]/description",
    widget="text",
    label="Omschrijving",
)

KEYCLOAK_REALM_ROLES = ProjectEditable(
    yaml_path="services/keycloak/config/realm-roles",
    widget="sequence",
    label="Realm rollen",
    children=[KEYCLOAK_ROLE_NAME, KEYCLOAK_ROLE_DESCRIPTION],
    min_items=0,
    max_items=10,
    help_text="Aangepaste rollen voor fijnmazige toegangscontrole.",
)

# ---------------------------------------------------------------------------
# Namespace PostgreSQL config
# ---------------------------------------------------------------------------

POSTGRESQL_INSTANCES = ProjectEditable(
    yaml_path="services/namespace-postgresql-database/config/instances",
    widget="number",
    label="Aantal instanties",
    validator=RangeValidator(min_value=1, max_value=5),
)

POSTGRESQL_STORAGE = ProjectEditable(
    yaml_path="services/namespace-postgresql-database/config/storage",
    widget="select",
    label="Opslaggrootte",
    options_provider="StorageSizeOptionsProvider",
)

# ---------------------------------------------------------------------------
# Authorization wall config
# ---------------------------------------------------------------------------

AUTH_WALL_BANNER = ProjectEditable(
    yaml_path="services/authorization-wall/config/banner",
    widget="textarea",
    label="Welkomstbanner tekst",
)

# ---------------------------------------------------------------------------
# Lookup for conditional section resolution
# ---------------------------------------------------------------------------

SERVICE_CONFIG_EDITABLES: dict[str, list[ProjectEditable]] = {
    "keycloak": [
        KEYCLOAK_TEMPLATE,
        KEYCLOAK_REDIRECT_URIS,
        KEYCLOAK_RESTRICT_ACCESS,
        KEYCLOAK_RESTRICT_ACCESS_ROLE,
        KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
        KEYCLOAK_ADDITIONAL_CLIENTS,
    ],
    "namespace-postgresql-database": [POSTGRESQL_INSTANCES, POSTGRESQL_STORAGE],
    "authorization-wall": [AUTH_WALL_BANNER],
}
