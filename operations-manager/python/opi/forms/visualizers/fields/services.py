"""Services visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.services import (
    KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE,
    KEYCLOAK_CLIENT_NAME_EDITABLE,
    KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE,
    KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE,
    KEYCLOAK_REALM_ROLES_EDITABLE,
    KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE,
    KEYCLOAK_REDIRECT_URIS_EDITABLE,
    KEYCLOAK_RESTRICT_ACCESS_EDITABLE,
    KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE,
    KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE,
    KEYCLOAK_ROLE_DESCRIPTION_EDITABLE,
    KEYCLOAK_ROLE_NAME_EDITABLE,
    KEYCLOAK_TEMPLATE_EDITABLE,
    SERVICES_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

# --- Service selection ---

SERVICES = EditableVisualizer(
    editable=SERVICES_EDITABLE,
    widget=WidgetType.SERVICE_CARDS,
    label="Beschikbare Services",
    description="Selecteer de services die u wilt activeren voor uw project",
)

# --- Keycloak ---

KEYCLOAK_TEMPLATE = EditableVisualizer(
    editable=KEYCLOAK_TEMPLATE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Keycloak template",
)

KEYCLOAK_REDIRECT_URI_ITEM = EditableVisualizer(
    editable=KEYCLOAK_REDIRECT_URI_ITEM_EDITABLE,
    widget=WidgetType.TEXT,
    label="URI",
)

KEYCLOAK_REDIRECT_URIS = EditableVisualizer(
    editable=KEYCLOAK_REDIRECT_URIS_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Extra redirect URI's",
    help_text="Redirect URI's voor lokale ontwikkeling of externe integraties.",
    children=[KEYCLOAK_REDIRECT_URI_ITEM],
)

KEYCLOAK_RESTRICT_ACCESS = EditableVisualizer(
    editable=KEYCLOAK_RESTRICT_ACCESS_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Toegang beperken",
    help_text="Wanneer ingeschakeld kunnen alleen gebruikers met de opgegeven realm-rol de applicatie openen",
    locked_by_service="authorization-wall",
    attributes={"data-rerender": "true"},
)

KEYCLOAK_RESTRICT_ACCESS_ROLE = EditableVisualizer(
    editable=KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Realm rol",
    help_text=(
        "De naam van de Keycloak realm-rol die toegang verleent. "
        "Elke gebruiker met deze rol mag de applicatie gebruiken. "
        "De rol wordt door de projectbeheerder aan gebruikers toegekend; "
        "de exacte naam maakt niet uit. Bij twijfel: gebruik de standaardwaarde."
    ),
)

KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG = EditableVisualizer(
    editable=KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE,
    widget=WidgetType.TEXT,
    label="Foutmelding",
    help_text=(
        "Bericht dat wordt getoond wanneer een gebruiker geen toegang heeft. "
        "De standaardwaarde gebruikt een vertaalbaar template. "
        "Bij twijfel: gebruik de standaardwaarde."
    ),
)

KEYCLOAK_CLIENT_NAME = EditableVisualizer(
    editable=KEYCLOAK_CLIENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Client naam",
)

KEYCLOAK_CLIENT_REDIRECT_URI = EditableVisualizer(
    editable=KEYCLOAK_CLIENT_REDIRECT_URI_EDITABLE,
    widget=WidgetType.TEXT,
    label="URI",
)

KEYCLOAK_CLIENT_REDIRECT_URIS = EditableVisualizer(
    editable=KEYCLOAK_CLIENT_REDIRECT_URIS_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Redirect URI's",
    children=[KEYCLOAK_CLIENT_REDIRECT_URI],
)

KEYCLOAK_ADDITIONAL_CLIENTS = EditableVisualizer(
    editable=KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Extra Keycloak clients",
    help_text="Extra clients voor microservices of externe applicaties die dezelfde realm delen.",
    children=[KEYCLOAK_CLIENT_NAME, KEYCLOAK_CLIENT_REDIRECT_URIS],
)

KEYCLOAK_ROLE_NAME = EditableVisualizer(
    editable=KEYCLOAK_ROLE_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Rolnaam",
)

KEYCLOAK_ROLE_DESCRIPTION = EditableVisualizer(
    editable=KEYCLOAK_ROLE_DESCRIPTION_EDITABLE,
    widget=WidgetType.TEXT,
    label="Omschrijving",
)

KEYCLOAK_REALM_ROLES = EditableVisualizer(
    editable=KEYCLOAK_REALM_ROLES_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Realm rollen",
    help_text="Aangepaste rollen voor fijnmazige toegangscontrole.",
    children=[KEYCLOAK_ROLE_NAME, KEYCLOAK_ROLE_DESCRIPTION],
)


# --- Authorization wall ---
