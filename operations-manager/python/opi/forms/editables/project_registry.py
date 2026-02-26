"""
Project editable definitions and form layout.

Re-exports all editables from domain-specific field modules and provides
the public API functions for form composition.
"""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.fields.components import (
    COMPONENT_ALIASES,
    COMPONENT_IMAGE,
    COMPONENT_NAME,
    COMPONENT_PORTS_INBOUND,
    COMPONENT_PORTS_OUTBOUND,
    COMPONENT_RESOURCES_CPU_LIMIT,
    COMPONENT_RESOURCES_CPU_REQUEST,
    COMPONENT_RESOURCES_MEMORY_LIMIT,
    COMPONENT_RESOURCES_MEMORY_REQUEST,
    COMPONENT_STORAGE_SEQUENCE,
    COMPONENT_USER_ENV_VARS,
    COMPONENT_USES_SERVICES,
    COMPONENTS_SEQUENCE,
)
from opi.forms.editables.fields.config_display import (
    AGE_PRIVATE_KEY,
    AGE_PUBLIC_KEY,
    API_KEY,
)
from opi.forms.editables.fields.deployments import (
    DEPLOYMENT_BASE_DOMAIN,
    DEPLOYMENT_CLONE_FROM,
    DEPLOYMENT_CLUSTER,
    DEPLOYMENT_COMP_IMAGE,
    DEPLOYMENT_COMP_PULL_POLICY,
    DEPLOYMENT_COMP_REFERENCE,
    DEPLOYMENT_COMPONENTS_SEQ,
    DEPLOYMENT_DOMAIN_MODE,
    DEPLOYMENT_NAME,
    DEPLOYMENT_REPOSITORY,
    DEPLOYMENT_SUBDOMAIN,
    DEPLOYMENTS_SEQUENCE,
)
from opi.forms.editables.fields.identity import (
    CLUSTERS,
    DESCRIPTION,
    DISPLAY_NAME,
)
from opi.forms.editables.fields.services import (
    AUTH_WALL_BANNER,
    KEYCLOAK_REDIRECT_URIS,
    KEYCLOAK_RESTRICT_ACCESS,
    KEYCLOAK_TEMPLATE,
    POSTGRESQL_INSTANCES,
    POSTGRESQL_STORAGE,
    SERVICE_CONFIG_EDITABLES,
    SERVICES,
)
from opi.forms.editables.fields.team import (
    USER_EMAIL,
    USER_ROLE,
    USERS_SEQUENCE,
)
from opi.forms.layout import (
    ButtonGroup,
    Fieldset,
    LayoutElement,
    Sequence,
    Submit,
)

# Re-export all editables for backward compatibility
__all__ = [
    # Identity
    "DISPLAY_NAME",
    "DESCRIPTION",
    "CLUSTERS",
    # Team
    "USER_EMAIL",
    "USER_ROLE",
    "USERS_SEQUENCE",
    # Services
    "SERVICES",
    "KEYCLOAK_TEMPLATE",
    "KEYCLOAK_REDIRECT_URIS",
    "KEYCLOAK_RESTRICT_ACCESS",
    "POSTGRESQL_INSTANCES",
    "POSTGRESQL_STORAGE",
    "AUTH_WALL_BANNER",
    "SERVICE_CONFIG_EDITABLES",
    # Components
    "COMPONENT_NAME",
    "COMPONENT_IMAGE",
    "COMPONENT_PORTS_INBOUND",
    "COMPONENT_PORTS_OUTBOUND",
    "COMPONENT_RESOURCES_CPU_REQUEST",
    "COMPONENT_RESOURCES_CPU_LIMIT",
    "COMPONENT_RESOURCES_MEMORY_REQUEST",
    "COMPONENT_RESOURCES_MEMORY_LIMIT",
    "COMPONENT_USES_SERVICES",
    "COMPONENT_ALIASES",
    "COMPONENT_USER_ENV_VARS",
    "COMPONENT_STORAGE_SEQUENCE",
    "COMPONENTS_SEQUENCE",
    # Deployments
    "DEPLOYMENT_NAME",
    "DEPLOYMENT_CLUSTER",
    "DEPLOYMENT_REPOSITORY",
    "DEPLOYMENT_SUBDOMAIN",
    "DEPLOYMENT_BASE_DOMAIN",
    "DEPLOYMENT_DOMAIN_MODE",
    "DEPLOYMENT_CLONE_FROM",
    "DEPLOYMENT_COMP_REFERENCE",
    "DEPLOYMENT_COMP_IMAGE",
    "DEPLOYMENT_COMP_PULL_POLICY",
    "DEPLOYMENT_COMPONENTS_SEQ",
    "DEPLOYMENTS_SEQUENCE",
    # Config display
    "AGE_PUBLIC_KEY",
    "AGE_PRIVATE_KEY",
    "API_KEY",
    # Public API
    "get_all_project_editables",
    "get_project_form_layout",
]


def get_all_project_editables() -> list[ProjectEditable]:
    """Return flat list of all top-level editables for the project form."""
    return [
        DISPLAY_NAME,
        DESCRIPTION,
        CLUSTERS,
        USERS_SEQUENCE,
        SERVICES,
        COMPONENTS_SEQUENCE,
        DEPLOYMENTS_SEQUENCE,
        AGE_PUBLIC_KEY,
        AGE_PRIVATE_KEY,
        API_KEY,
    ]


def get_project_form_layout() -> list[LayoutElement]:
    """Return layout definition for the single-page project form."""
    return [
        Fieldset(
            legend="Projectgegevens",
            children=[
                "display-name",
                "description",
                "clusters",
            ],
        ),
        Fieldset(
            legend="Projectleden",
            children=[
                Sequence(field_name="users"),
            ],
        ),
        Fieldset(
            legend="Services",
            children=[
                "services",
            ],
        ),
        Fieldset(
            legend="Componenten",
            children=[
                Sequence(field_name="components"),
            ],
        ),
        Fieldset(
            legend="Deployments",
            children=[
                Sequence(field_name="deployments"),
            ],
        ),
        Fieldset(
            legend="Configuratie",
            description="Automatisch gegenereerde configuratie (alleen-lezen)",
            children=[
                "config/age-public-key",
                "config/age-private-key",
                "config/api-key",
            ],
        ),
        ButtonGroup(
            buttons=[
                Submit(label="Opslaan", kind="primary"),
            ]
        ),
    ]
