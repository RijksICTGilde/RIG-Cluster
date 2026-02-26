"""Components section editables: component definition fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    ContainerImageConverter,
    IntegerConverter,
    KeyValueConverter,
)
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import (
    AllowedValuesValidator,
    ComponentNameValidator,
    ContainerImageValidator,
    PathValidator,
)

COMPONENT_NAME = ProjectEditable(
    yaml_path="components[*]/name",
    widget="text",
    label="Componentnaam",
    required=True,
    validator=ComponentNameValidator(),
    description="Alleen kleine letters en cijfers, maximaal 12 tekens.",
    help_text="Voorbeeld: frontend, api, worker.",
)

COMPONENT_IMAGE = ProjectEditable(
    yaml_path="components[*]/image",
    widget="text",
    label="Container image",
    validator=ContainerImageValidator(),
    converter=ContainerImageConverter(),
    description="Docker image van uw applicatie. Moet een rootless image zijn.",
    help_text=(
        "Bijvoorbeeld: nginx:latest, python:3.13-slim. "
        "Kan leeg gelaten worden; er wordt dan geen deployment aangemaakt voor dit component."
    ),
    help_template="container-image.html.j2",
)

# Port sub-fields (each port is a single integer in a list)
INBOUND_PORT = ProjectEditable(
    yaml_path="components[*]/ports/inbound[*]",
    widget="text",
    label="Poort",
    converter=IntegerConverter(),
)

COMPONENT_PORTS_INBOUND = ProjectEditable(
    yaml_path="components[*]/ports/inbound",
    widget="sequence",
    label="Inbound poorten",
    children=[INBOUND_PORT],
    default=[8080],
    description="Poorten waarop het component luistert (bijv. 8080, 3000).",
)

OUTBOUND_PORT = ProjectEditable(
    yaml_path="components[*]/ports/outbound[*]",
    widget="text",
    label="Poort",
    converter=IntegerConverter(),
)

COMPONENT_PORTS_OUTBOUND = ProjectEditable(
    yaml_path="components[*]/ports/outbound",
    widget="sequence",
    label="Outbound poorten",
    children=[OUTBOUND_PORT],
    default=[80, 443],
    help_text="Poorten waarnaar het component uitgaand verkeer stuurt",
)

COMPONENT_RESOURCES_CPU_REQUEST = ProjectEditable(
    yaml_path="components[*]/resources/cpu/request",
    widget="select",
    label="CPU request",
    description="Gegarandeerd CPU-gebruik",
    options_provider="CpuRequestOptionsProvider",
    validator=AllowedValuesValidator(["50m", "100m", "250m", "500m"]),
    default="50m",
    help_text="Het minimale CPU-gebruik dat Kubernetes garandeert voor dit component.",
)

COMPONENT_RESOURCES_CPU_LIMIT = ProjectEditable(
    yaml_path="components[*]/resources/cpu/limit",
    widget="select",
    label="CPU limiet",
    description="Maximaal CPU-gebruik",
    options_provider="CpuLimitOptionsProvider",
    validator=AllowedValuesValidator(["500m", "1"]),
    default="500m",
    help_text="Het maximale CPU-gebruik. Bij overschrijding wordt het component beperkt (throttled).",
)

COMPONENT_RESOURCES_MEMORY_REQUEST = ProjectEditable(
    yaml_path="components[*]/resources/memory/request",
    widget="select",
    label="Geheugen request",
    description="Gegarandeerd geheugengebruik",
    options_provider="MemoryRequestOptionsProvider",
    validator=AllowedValuesValidator(["256Mi", "512Mi"]),
    default="256Mi",
    help_text="Het minimale geheugen dat Kubernetes garandeert voor dit component.",
)

COMPONENT_RESOURCES_MEMORY_LIMIT = ProjectEditable(
    yaml_path="components[*]/resources/memory/limit",
    widget="select",
    label="Geheugen limiet",
    description="Maximaal geheugengebruik",
    options_provider="MemoryLimitOptionsProvider",
    validator=AllowedValuesValidator(["512Mi", "768Mi", "1Gi"]),
    default="512Mi",
    help_text="Het maximale geheugen. Bij overschrijding wordt het component herstart (OOMKilled).",
)

COMPONENT_USES_SERVICES = ProjectEditable(
    yaml_path="components[*]/uses-services",
    widget="checkbox_group",
    label="Gebruikte services",
    options_provider="FilteredServiceOptionsProvider",
    default="__all__",
    help_text=(
        "Hiermee worden de juiste omgevingsvariabelen en netwerktoegang geconfigureerd."
    ),
)

COMPONENT_PATH = ProjectEditable(
    yaml_path="components[*]/path",
    widget="text",
    label="Publicatie pad",
    default="/",
    validator=PathValidator(),
    description="Het pad waarop dit component gepubliceerd wordt.",
    help_text=(
        "Bijvoorbeeld: /, /api, /admin. "
        "Bij gedeelde domeinen wordt dit pad gebruikt om verkeer naar het juiste component te routeren. Gebruik de standaardwaarde als je niet zeker weet wat je moet invullen."
    ),
)

COMPONENT_REWRITE_PATH = ProjectEditable(
    yaml_path="components[*]/rewrite-path",
    widget="text",
    label="Rewrite pad",
    validator=PathValidator(),
    description=("Optioneel. Meestal niet nodig. Gebruik de standaardwaarde tenzij je zeker weet wat je moet invullen."),
    help_text=(
        "Zonder rewrite ontvangt uw container het volledige pad uit de URL. "
        "Bijvoorbeeld: publicatie pad /api betekent dat een request naar /api/users "
        "ook als /api/users bij de container aankomt. "
        "Een rewrite is alleen nodig als uw applicatie het prefix-pad niet verwacht. "
        "Bijvoorbeeld: uw app luistert op / maar wordt gepubliceerd op /api — "
        "stel dan rewrite pad in op / zodat /api/users wordt herschreven naar /users "
        "voordat het de container bereikt."
    ),
)

COMPONENT_ALIASES = ProjectEditable(
    yaml_path="components[*]/aliases",
    widget="key_value_editor",
    label="Aliassen",
    description="Koppel platform-variabelen aan aangepaste namen voor dit component.",
    converter=KeyValueConverter(fmt="env"),
    help_text=(
        "Wijs omgevingsvariabelen toe aan andere namen. "
        "Gebruik $VARIABELE_NAAM om platform-variabelen te refereren. "
        "Bijvoorbeeld: POSTGRES_HOST=$DATABASE_SERVER_HOST"
    ),
    attributes={"kv_format": "env"},
)

COMPONENT_USER_ENV_VARS = ProjectEditable(
    yaml_path="components[*]/user-env-vars",
    widget="key_value_editor",
    label="Eigen omgevingsvariabelen",
    description="Voeg eigen omgevingsvariabelen toe voor dit component.",
    converter=KeyValueConverter(fmt="env"),
    help_text=(
        "Definieer extra omgevingsvariabelen die beschikbaar worden in de container. "
        "Deze waarden worden versleuteld opgeslagen. "
        "Bijvoorbeeld: API_KEY=mijn-geheime-sleutel"
    ),
    attributes={"kv_format": "env"},
)

# Storage sub-fields for components
STORAGE_NAME = ProjectEditable(
    yaml_path="components[*]/storage[*]/name",
    widget="text",
    label="Naam",
    required=True,
    help_text="Unieke naam voor dit opslagvolume binnen het component.",
)

STORAGE_TYPE = ProjectEditable(
    yaml_path="components[*]/storage[*]/type",
    widget="select",
    label="Type",
    options_provider="StorageTypeOptionsProvider",
    help_text="Persistent: data blijft behouden bij herstarts. Ephemeral: tijdelijke opslag, gaat verloren bij herstarts.",
)

STORAGE_SIZE = ProjectEditable(
    yaml_path="components[*]/storage[*]/size",
    widget="select",
    label="Grootte",
    options_provider="StorageSizeOptionsProvider",
    help_text="De maximale grootte van het opslagvolume.",
)

STORAGE_MOUNT_PATH = ProjectEditable(
    yaml_path="components[*]/storage[*]/mount-path",
    widget="text",
    label="Mount pad",
    required=True,
    help_text="Het pad in de container waar het volume wordt gemount (bijv. /data, /var/lib/app).",
)

COMPONENT_STORAGE_SEQUENCE = ProjectEditable(
    yaml_path="components[*]/storage",
    widget="sequence",
    label="Opslagvolumes",
    children=[STORAGE_NAME, STORAGE_TYPE, STORAGE_SIZE, STORAGE_MOUNT_PATH],
    depends_on="services",
    show_when={"contains_any": ["persistent-storage", "temp-storage"]},
    help_text="Persistente of tijdelijke opslagvolumes die in de container worden gemount",
)

COMPONENTS_SEQUENCE = ProjectEditable(
    yaml_path="components",
    widget="sequence",
    label="Componenten",
    min_items=1,
    children=[
        COMPONENT_NAME,
        COMPONENT_IMAGE,
        COMPONENT_RESOURCES_CPU_REQUEST,
        COMPONENT_RESOURCES_CPU_LIMIT,
        COMPONENT_RESOURCES_MEMORY_REQUEST,
        COMPONENT_RESOURCES_MEMORY_LIMIT,
        COMPONENT_PORTS_INBOUND,
        COMPONENT_PORTS_OUTBOUND,
        COMPONENT_USES_SERVICES,
        COMPONENT_PATH,
        COMPONENT_REWRITE_PATH,
        COMPONENT_ALIASES,
        COMPONENT_USER_ENV_VARS,
        COMPONENT_STORAGE_SEQUENCE,
    ],
)
