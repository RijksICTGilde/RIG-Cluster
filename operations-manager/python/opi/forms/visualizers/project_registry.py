"""
Project editable definitions and form layout.

Re-exports all visualizer constants from field modules and provides
the public API functions for form composition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.forms.layout import (
    ButtonGroup,
    Fieldset,
    LayoutElement,
    Sequence,
    Submit,
)
from opi.forms.visualizers.fields.components import (
    COMPONENT_ALIASES,
    COMPONENT_IMAGE,
    COMPONENT_NAME,
    COMPONENT_PORTS_INBOUND,
    COMPONENT_PORTS_OUTBOUND,
    COMPONENT_RESOURCES_CPU_LIMIT,
    COMPONENT_RESOURCES_CPU_REQUEST,
    COMPONENT_RESOURCES_MEMORY_LIMIT,
    COMPONENT_RESOURCES_MEMORY_REQUEST,
    COMPONENT_SERVICES,
    COMPONENT_USER_ENV_VARS,
    COMPONENTS_SEQUENCE,
)
from opi.forms.visualizers.fields.config_display import (
    AGE_PRIVATE_KEY,
    AGE_PUBLIC_KEY,
    API_KEY,
)
from opi.forms.visualizers.fields.deployments import (
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
from opi.forms.visualizers.fields.identity import (
    CLUSTERS,
    DESCRIPTION,
    DISPLAY_NAME,
)
from opi.forms.visualizers.fields.services import (
    SERVICES,
)
from opi.forms.visualizers.fields.team import (
    USER_EMAIL,
    USER_ROLE,
    USERS_SEQUENCE,
)

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer

# Re-export all visualizer constants for backward compatibility
__all__ = [
    "AGE_PRIVATE_KEY",
    "AGE_PUBLIC_KEY",
    "API_KEY",
    "CLUSTERS",
    "COMPONENTS_SEQUENCE",
    "COMPONENT_ALIASES",
    "COMPONENT_IMAGE",
    "COMPONENT_NAME",
    "COMPONENT_PORTS_INBOUND",
    "COMPONENT_PORTS_OUTBOUND",
    "COMPONENT_RESOURCES_CPU_LIMIT",
    "COMPONENT_RESOURCES_CPU_REQUEST",
    "COMPONENT_RESOURCES_MEMORY_LIMIT",
    "COMPONENT_RESOURCES_MEMORY_REQUEST",
    "COMPONENT_SERVICES",
    "COMPONENT_USER_ENV_VARS",
    "DEPLOYMENTS_SEQUENCE",
    "DEPLOYMENT_BASE_DOMAIN",
    "DEPLOYMENT_CLONE_FROM",
    "DEPLOYMENT_CLUSTER",
    "DEPLOYMENT_COMPONENTS_SEQ",
    "DEPLOYMENT_COMP_IMAGE",
    "DEPLOYMENT_COMP_PULL_POLICY",
    "DEPLOYMENT_COMP_REFERENCE",
    "DEPLOYMENT_DOMAIN_MODE",
    "DEPLOYMENT_NAME",
    "DEPLOYMENT_REPOSITORY",
    "DEPLOYMENT_SUBDOMAIN",
    "DESCRIPTION",
    "DISPLAY_NAME",
    "SERVICES",
    "USERS_SEQUENCE",
    "USER_EMAIL",
    "USER_ROLE",
    "get_all_project_editables",
    "get_project_form_layout",
]


def get_all_project_editables() -> list[EditableVisualizer]:
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
