"""
Project editable definitions and form layout.

Declares all ProjectEditable instances for the project YAML structure.
Only user-editable and display-worthy fields — NOT auto-generated fields
like repository URLs, git credentials, or AGE key generation.
"""

from __future__ import annotations

from opi.forms.editables.converters import (
    EncryptedDisplayConverter,
    IntegerListConverter,
    KeyValueConverter,
    ServiceListConverter,
    TruncateConverter,
)
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import EmailValidator, MinMaxLengthValidator, SlugValidator
from opi.forms.layout import (
    ButtonGroup,
    Column,
    Fieldset,
    LayoutElement,
    Row,
    Sequence,
    Submit,
)

# ---------------------------------------------------------------------------
# Identity Section
# ---------------------------------------------------------------------------

NAME = ProjectEditable(
    yaml_path="name",
    widget="text",
    label="Projectnaam (technisch)",
    description="Technische identificatie, kan niet gewijzigd worden",
    required=True,
    readonly_on_edit=True,
    validator=SlugValidator(),
)

DISPLAY_NAME = ProjectEditable(
    yaml_path="display-name",
    widget="text",
    label="Weergavenaam",
    description="Een beschrijvende naam voor uw project",
    required=True,
    placeholder="Mijn Nieuwe Applicatie",
    validator=MinMaxLengthValidator(3, 100),
)

DESCRIPTION = ProjectEditable(
    yaml_path="description",
    widget="textarea",
    label="Projectomschrijving",
    description="Korte beschrijving van het doel en de scope van het project",
    placeholder="Dit project heeft als doel...",
)

CLUSTERS = ProjectEditable(
    yaml_path="clusters",
    widget="checkbox_group",
    label="Clusters",
    description="Selecteer de clusters waar dit project op draait",
    options_provider="ClusterOptionsProvider",
    required=True,
)

# ---------------------------------------------------------------------------
# Team Section
# ---------------------------------------------------------------------------

USER_EMAIL = ProjectEditable(
    yaml_path="users[*]/email",
    widget="text",
    label="E-mailadres",
    required=True,
    placeholder="naam@organisatie.nl",
    validator=EmailValidator(),
)

USER_ROLE = ProjectEditable(
    yaml_path="users[*]/role",
    widget="select",
    label="Rol",
    required=True,
    options_provider="UserRoleOptionsProvider",
)

USERS_SEQUENCE = ProjectEditable(
    yaml_path="users",
    widget="sequence",
    label="Projectleden",
    min_items=1,
    children=[USER_EMAIL, USER_ROLE],
)

# ---------------------------------------------------------------------------
# Services Section
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
# Components Section
# ---------------------------------------------------------------------------

COMPONENT_NAME = ProjectEditable(
    yaml_path="components[*]/name",
    widget="text",
    label="Naam",
    required=True,
    validator=SlugValidator(),
)

COMPONENT_TYPE = ProjectEditable(
    yaml_path="components[*]/type",
    widget="select",
    label="Type",
    required=True,
    options_provider="ComponentTypeOptionsProvider",
)

COMPONENT_PORTS_INBOUND = ProjectEditable(
    yaml_path="components[*]/ports/inbound",
    widget="text",
    label="Inbound poorten",
    description="Kommagescheiden lijst van poorten (bijv. 8000, 8080)",
    converter=IntegerListConverter(),
)

COMPONENT_PORTS_OUTBOUND = ProjectEditable(
    yaml_path="components[*]/ports/outbound",
    widget="text",
    label="Outbound poorten",
    description="Kommagescheiden lijst (bijv. 80, 443)",
    converter=IntegerListConverter(),
)

COMPONENT_RESOURCES_CPU = ProjectEditable(
    yaml_path="components[*]/resources/cpu",
    widget="select",
    label="CPU limiet",
    options_provider="CpuLimitOptionsProvider",
)

COMPONENT_RESOURCES_MEMORY = ProjectEditable(
    yaml_path="components[*]/resources/memory",
    widget="select",
    label="Geheugen limiet",
    options_provider="MemoryLimitOptionsProvider",
)

COMPONENT_USES_SERVICES = ProjectEditable(
    yaml_path="components[*]/uses-services",
    widget="checkbox_group",
    label="Gebruikte services",
    description="Welke project-services gebruikt dit component",
    options_provider="FilteredServiceOptionsProvider",
)

COMPONENT_ALIASES = ProjectEditable(
    yaml_path="components[*]/aliases",
    widget="textarea",
    label="Aliassen",
    description="Variabele aliassen in KEY=VALUE formaat",
    converter=KeyValueConverter(),
)

COMPONENTS_SEQUENCE = ProjectEditable(
    yaml_path="components",
    widget="sequence",
    label="Componenten",
    min_items=1,
    children=[
        COMPONENT_NAME,
        COMPONENT_TYPE,
        COMPONENT_PORTS_INBOUND,
        COMPONENT_PORTS_OUTBOUND,
        COMPONENT_RESOURCES_CPU,
        COMPONENT_RESOURCES_MEMORY,
        COMPONENT_USES_SERVICES,
        COMPONENT_ALIASES,
    ],
)

# ---------------------------------------------------------------------------
# Deployments Section
# ---------------------------------------------------------------------------

DEPLOYMENT_NAME = ProjectEditable(
    yaml_path="deployments[*]/name",
    widget="text",
    label="Deployment naam",
    required=True,
    readonly_on_edit=True,
)

DEPLOYMENT_CLUSTER = ProjectEditable(
    yaml_path="deployments[*]/cluster",
    widget="select",
    label="Cluster",
    required=True,
    options_provider="ClusterOptionsProvider",
)

DEPLOYMENT_REPOSITORY = ProjectEditable(
    yaml_path="deployments[*]/repository",
    widget="select",
    label="Repository",
    options_provider="RepositoryOptionsProvider",
)

DEPLOYMENT_SUBDOMAIN = ProjectEditable(
    yaml_path="deployments[*]/subdomain",
    widget="text",
    label="Subdomein",
    description="Optioneel subdomein voor deze deployment",
)

DEPLOYMENT_COMP_REFERENCE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/reference",
    widget="select",
    label="Component",
    required=True,
    options_provider="ComponentReferenceOptionsProvider",
)

DEPLOYMENT_COMP_IMAGE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/image",
    widget="text",
    label="Container image",
    required=True,
    placeholder="nginx:latest",
)

DEPLOYMENT_COMP_PULL_POLICY = ProjectEditable(
    yaml_path="deployments[*]/components[*]/imagePullPolicy",
    widget="select",
    label="Pull policy",
    options_provider="PullPolicyOptionsProvider",
)

DEPLOYMENT_COMPONENTS_SEQ = ProjectEditable(
    yaml_path="deployments[*]/components",
    widget="sequence",
    label="Deployment componenten",
    min_items=1,
    children=[DEPLOYMENT_COMP_REFERENCE, DEPLOYMENT_COMP_IMAGE, DEPLOYMENT_COMP_PULL_POLICY],
)

DEPLOYMENTS_SEQUENCE = ProjectEditable(
    yaml_path="deployments",
    widget="sequence",
    label="Deployments",
    children=[
        DEPLOYMENT_NAME,
        DEPLOYMENT_CLUSTER,
        DEPLOYMENT_REPOSITORY,
        DEPLOYMENT_SUBDOMAIN,
        DEPLOYMENT_COMPONENTS_SEQ,
    ],
)

# ---------------------------------------------------------------------------
# Config Section (Read-Only Display)
# ---------------------------------------------------------------------------

AGE_PUBLIC_KEY = ProjectEditable(
    yaml_path="config/age-public-key",
    widget="display_card",
    label="AGE publieke sleutel",
    readonly=True,
    converter=TruncateConverter(20),
)

AGE_PRIVATE_KEY = ProjectEditable(
    yaml_path="config/age-private-key",
    widget="display_card",
    label="AGE priv\u00e9 sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)

API_KEY = ProjectEditable(
    yaml_path="config/api-key",
    widget="display_card",
    label="API sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_project_editables() -> list[ProjectEditable]:
    """Return flat list of all top-level editables for the project form."""
    return [
        NAME,
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
                Row(
                    children=[
                        Column(child="name", width=4),
                        Column(child="display-name", width=8),
                    ]
                ),
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
                Submit(label="Opslaan", kind="primary", icon="opslaan"),
            ]
        ),
    ]
