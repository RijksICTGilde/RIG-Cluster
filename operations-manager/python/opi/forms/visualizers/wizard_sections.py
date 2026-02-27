"""Wizard section definitions for project forms.

Each FormSection groups related editables into a logical wizard step.
Step ordering is determined by the sections list in FormFlow, NOT by
any property on the section itself.
"""

from __future__ import annotations

from typing import Any

from opi.forms.layout import Fieldset, Sequence, TemplatePartial
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.forms.visualizers.fields.config_display import AGE_PRIVATE_KEY, AGE_PUBLIC_KEY, API_KEY
from opi.forms.visualizers.fields.deployments import DEPLOYMENTS_SEQUENCE
from opi.forms.visualizers.fields.domains import (
    DOMAIN_BASE_DOMAIN,
    DOMAIN_MODE,
    DOMAIN_ROOT_COMPONENT,
    DOMAIN_SUBDOMAIN,
    WIZARD_DEPLOYMENT_NAME,
)
from opi.forms.visualizers.fields.identity import CLUSTERS, DESCRIPTION, DISPLAY_NAME
from opi.forms.visualizers.fields.services import (
    AUTH_WALL_BANNER,
    KEYCLOAK_ADDITIONAL_CLIENTS,
    KEYCLOAK_REDIRECT_URIS,
    KEYCLOAK_RESTRICT_ACCESS,
    KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
    KEYCLOAK_RESTRICT_ACCESS_ROLE,
    KEYCLOAK_TEMPLATE,
    POSTGRESQL_INSTANCES,
    POSTGRESQL_STORAGE,
    SERVICES,
)
from opi.forms.visualizers.fields.team import USERS_SEQUENCE
from opi.forms.visualizers.sections import FormSection


def _extract_services(data: dict[str, Any]) -> list[str]:
    """Extract active service names from wizard form data.

    Handles all formats in the services list:
      - strings: "keycloak"
      - service-keyed dicts: {"keycloak": {"config": ...}}
      - legacy name dicts: {"name": "keycloak"}
    """
    services = data.get("services", [])
    if isinstance(services, list):
        result: list[str] = []
        for svc in services:
            if isinstance(svc, str):
                result.append(svc)
            elif isinstance(svc, dict):
                if "name" in svc:
                    result.append(svc["name"])
                else:
                    result.extend(svc.keys())
        return result
    return []


# ---------------------------------------------------------------------------
# Core sections (always visible)
# ---------------------------------------------------------------------------

IDENTITY_SECTION = FormSection(
    section_id="identity",
    title="Projectgegevens",
    icon="document-blanco",
    description="Basisinformatie over uw project",
    editables=[DISPLAY_NAME, DESCRIPTION, CLUSTERS],
    layout=[
        "display-name",
        "description",
        "clusters",
    ],
)

SERVICES_SECTION = FormSection(
    section_id="services",
    title="Services",
    icon="applicatie",
    description="Selecteer de services die u wilt activeren",
    editables=[SERVICES],
    layout=["services"],
)

TEAM_SECTION = FormSection(
    section_id="team",
    title="Projectleden",
    icon="groep-3-personen",
    description="Beheer teamleden",
    editables=[USERS_SEQUENCE],
    layout=[Sequence(field_name="users")],
)

COMPONENTS_SECTION = FormSection(
    section_id="components",
    title="Componenten",
    icon="puzzel",
    description="Definieer de applicatiecomponenten",
    editables=[COMPONENTS_SEQUENCE],
    layout=[
        Sequence(
            field_name="components",
            child_layout=[
                Fieldset(
                    legend="Identificatie",
                    children=[
                        "name",
                        "image",
                    ],
                ),
                Fieldset(
                    legend="Resources",
                    description="Geheugen limieten voor dit component. Gebruik de standaardwaarden als je niet zeker weet wat je nodig hebt. Dit kan later aangepast worden. "
                    "Deze waardes zijn een richtlijn, de waardes zullen aangepast worden aan het daadwerkelijke gebruik.",
                    children=[
                        "resources/memory/request",
                        "resources/memory/limit",
                    ],
                ),
                Fieldset(
                    legend="Netwerk",
                    description="Poorten waarop het component luistert voor inkomend verkeer.",
                    children=[
                        Sequence(field_name="ports/inbound"),
                    ],
                ),
                Fieldset(
                    legend="Services",
                    description="Selecteer welke services dit component gebruikt.",
                    children=["uses-services"],
                ),
                Fieldset(
                    legend="Publicatie",
                    description=(
                        "Bij gedeelde domeinen bepaalt het pad welk component het verkeer ontvangt. "
                        "Bijvoorbeeld: / voor de frontend en /api voor de backend."
                    ),
                    children=[
                        "path",
                        "rewrite-path",
                    ],
                ),
                Fieldset(
                    legend="Variabelen",
                    description="Omgevingsvariabelen en aliassen voor dit component.",
                    children=[
                        "aliases",
                        "user-env-vars",
                    ],
                ),
                Sequence(field_name="storage"),
            ],
        ),
    ],
)

DEPLOYMENTS_SECTION = FormSection(
    section_id="deployments",
    title="Deployments",
    icon="server",
    description="Configureer de deployment-omgevingen",
    editables=[DEPLOYMENTS_SEQUENCE],
    layout=[Sequence(field_name="deployments")],
)

CONFIG_DISPLAY_SECTION = FormSection(
    section_id="config",
    title="Configuratie",
    icon="instellingen",
    description="Automatisch gegenereerde configuratie (alleen-lezen)",
    is_readonly=True,
    editables=[AGE_PUBLIC_KEY, AGE_PRIVATE_KEY, API_KEY],
    layout=[
        "config/age-public-key",
        "config/age-private-key",
        "config/api-key",
    ],
)

# ---------------------------------------------------------------------------
# Conditional sections (visible based on selected services)
# ---------------------------------------------------------------------------

KEYCLOAK_CONFIG_SECTION = FormSection(
    section_id="keycloak-config",
    title="Keycloak configuratie",
    icon="sleutel",
    description="SSO en authenticatie-instellingen",
    visible=lambda data: "keycloak" in _extract_services(data),
    editables=[
        KEYCLOAK_TEMPLATE,
        KEYCLOAK_REDIRECT_URIS,
        KEYCLOAK_RESTRICT_ACCESS,
        KEYCLOAK_RESTRICT_ACCESS_ROLE,
        KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
        KEYCLOAK_ADDITIONAL_CLIENTS,
    ],
    layout=[
        Fieldset(
            legend="Template",
            children=["services/keycloak/config/template"],
        ),
        # Hidden: redirect URIs are managed via additional clients instead.
        # Fieldset(
        #     legend="Extra redirect-URI\u2019s",
        #     ...
        #     children=[Sequence(field_name="services/keycloak/config/additional_redirect_uris")],
        # ),
        Fieldset(
            legend="Toegangsbeperking",
            description="Beperk toegang tot de applicatie op basis van Keycloak realm-rollen.",
            children=[
                "services/keycloak/config/restrict-access/enabled",
                "services/keycloak/config/restrict-access/realm-role",
                "services/keycloak/config/restrict-access/error-message",
            ],
        ),
        Fieldset(
            legend="Extra Keycloak clients",
            description=(
                "Voeg extra clients toe als er externe applicaties zijn "
                "die het Keycloak realm van dit project gebruiken. Elke client krijgt een eigen client-ID "
                "en redirect URI's."
            ),
            children=[
                Sequence(field_name="services/keycloak/config/additional-clients"),
            ],
        ),
    ],
)

POSTGRESQL_CONFIG_SECTION = FormSection(
    section_id="postgresql-config",
    title="Database configuratie",
    icon="database",
    description="PostgreSQL database-instellingen",
    visible=lambda data: "namespace-postgresql-database" in _extract_services(data),
    editables=[POSTGRESQL_INSTANCES, POSTGRESQL_STORAGE],
    layout=[
        "services/namespace-postgresql-database/config/instances",
        "services/namespace-postgresql-database/config/storage",
    ],
)

DOMAIN_SECTION = FormSection(
    section_id="domains",
    title="Webadres",
    icon="wereld",
    description="Configureer hoe uw applicatie bereikbaar wordt",
    editables=[DOMAIN_MODE, DOMAIN_SUBDOMAIN, DOMAIN_BASE_DOMAIN, DOMAIN_ROOT_COMPONENT],
    layout=[
        TemplatePartial(template="wizard/partials/domain_info.html.j2"),
        "deployments[0]/domain-mode",
        "deployments[0]/subdomain",
        "deployments[0]/base-domain",
        "deployments[0]/root-component",
    ],
)

WIZARD_DEPLOYMENT_SECTION = FormSection(
    section_id="deployment",
    title="Deployment",
    icon="server",
    description="Configureer de deployment voor uw applicatie",
    editables=[WIZARD_DEPLOYMENT_NAME],
    layout=[
        TemplatePartial(template="wizard/partials/deployment_info.html.j2"),
        "deployments[0]/name",
    ],
)

AUTH_WALL_CONFIG_SECTION = FormSection(
    section_id="auth-wall-config",
    title="Authorization wall configuratie",
    icon="sleutel",
    description="Instellingen voor de toegangspagina",
    visible=lambda data: "authorization-wall" in _extract_services(data),
    editables=[AUTH_WALL_BANNER],
    layout=["services/authorization-wall/config/banner"],
)

# ---------------------------------------------------------------------------
# Lookup for conditional sections keyed by service name
# ---------------------------------------------------------------------------

SERVICE_CONFIG_SECTIONS: dict[str, FormSection] = {
    "keycloak": KEYCLOAK_CONFIG_SECTION,
    "namespace-postgresql-database": POSTGRESQL_CONFIG_SECTION,
    "authorization-wall": AUTH_WALL_CONFIG_SECTION,
}

# ---------------------------------------------------------------------------
# All sections for easy iteration
# ---------------------------------------------------------------------------

ALL_SECTIONS: list[FormSection] = [
    IDENTITY_SECTION,
    SERVICES_SECTION,
    KEYCLOAK_CONFIG_SECTION,
    POSTGRESQL_CONFIG_SECTION,
    AUTH_WALL_CONFIG_SECTION,
    TEAM_SECTION,
    COMPONENTS_SECTION,
    DOMAIN_SECTION,
    WIZARD_DEPLOYMENT_SECTION,
    DEPLOYMENTS_SECTION,
    CONFIG_DISPLAY_SECTION,
]
