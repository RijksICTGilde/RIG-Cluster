"""Components visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.components import (
    ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ATTACHMENT_USE_PATH_EDITABLE,
    ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    ATTACHMENT_USE_REFERENCE_EDITABLE,
    ATTACHMENT_USE_SEQUENCE_EDITABLE,
    COMPONENT_ALIASES_EDITABLE,
    COMPONENT_IMAGE_EDITABLE,
    COMPONENT_NAME_EDITABLE,
    COMPONENT_PATH_EDITABLE,
    COMPONENT_PATH_MATCH_EDITABLE,
    COMPONENT_PATH_REWRITE_EDITABLE,
    COMPONENT_PORTS_INBOUND_EDITABLE,
    COMPONENT_PORTS_OUTBOUND_EDITABLE,
    COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE,
    COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
    COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE,
    COMPONENT_SERVICES_EDITABLE,
    COMPONENT_USER_ENV_VARS_EDITABLE,
    COMPONENTS_SEQUENCE_EDITABLE,
    INBOUND_PORT_EDITABLE,
    METRICS_PATH_EDITABLE,
    METRICS_PORT_EDITABLE,
    OUTBOUND_PORT_EDITABLE,
    PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    PERSISTENT_STORAGE_NAME_EDITABLE,
    PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
    PERSISTENT_STORAGE_SIZE_EDITABLE,
    PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    PUBLISH_ON_WEB_TLS_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

# temp-storage's visualizer lives in its service package; imported here only so the
# component-form aggregation (COMPONENTS_SEQUENCE) can reference it.
from opi.services.catalog.temp_storage.visualizers import TEMP_STORAGE_SEQUENCE

COMPONENT_NAME = EditableVisualizer(
    editable=COMPONENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Componentnaam",
    description=(
        "Alleen kleine letters, cijfers en koppeltekens, maximaal 63 tekens. "
        "Begint met een letter en eindigt niet op een koppelteken."
    ),
    help_text="Voorbeeld: frontend, api, worker.",
    readonly_on_edit=True,
)

COMPONENT_IMAGE = EditableVisualizer(
    editable=COMPONENT_IMAGE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Container image",
    description="Docker image van uw applicatie. Moet een rootless image zijn.",
    help_text=(
        "Bijvoorbeeld: ghcr.io/minbzk/base-images/hello-world:latest."
        "Kan leeg gelaten worden; er wordt dan geen deployment aangemaakt voor dit component."
    ),
    help_template="container-image.html.j2",
    attributes={"data-paste-clean": "container-image"},
)

INBOUND_PORT = EditableVisualizer(
    editable=INBOUND_PORT_EDITABLE,
    widget=WidgetType.TEXT,
    label="Poort",
)

COMPONENT_PORTS_INBOUND = EditableVisualizer(
    editable=COMPONENT_PORTS_INBOUND_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Inbound poorten",
    description="Poorten waarop het component luistert (bijv. 8080, 3000).",
    children=[INBOUND_PORT],
)

OUTBOUND_PORT = EditableVisualizer(
    editable=OUTBOUND_PORT_EDITABLE,
    widget=WidgetType.TEXT,
    label="Poort",
)

COMPONENT_PORTS_OUTBOUND = EditableVisualizer(
    editable=COMPONENT_PORTS_OUTBOUND_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Outbound poorten",
    help_text="Poorten waarnaar het component uitgaand verkeer stuurt",
    children=[OUTBOUND_PORT],
)

COMPONENT_RESOURCES_CPU_REQUEST = EditableVisualizer(
    editable=COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE,
    widget=WidgetType.SELECT,
    label="CPU request",
    description="Gegarandeerd CPU-gebruik",
    help_text="Het minimale CPU-gebruik dat Kubernetes garandeert voor dit component.",
)

COMPONENT_RESOURCES_CPU_LIMIT = EditableVisualizer(
    editable=COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    widget=WidgetType.SELECT,
    label="CPU limiet",
    description="Maximaal CPU-gebruik",
    help_text="Het maximale CPU-gebruik. Bij overschrijding wordt het component beperkt (throttled).",
)

COMPONENT_RESOURCES_MEMORY_REQUEST = EditableVisualizer(
    editable=COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE,
    widget=WidgetType.SELECT,
    label="Geheugen request",
    description="Gegarandeerd geheugengebruik",
    help_text="Het minimale geheugen dat Kubernetes garandeert voor dit component.",
)

COMPONENT_RESOURCES_MEMORY_LIMIT = EditableVisualizer(
    editable=COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Geheugen limiet",
    description="Maximaal geheugengebruik",
    help_text="Het maximale geheugen. Bij overschrijding wordt het component herstart (OOMKilled).",
)

COMPONENT_SERVICES = EditableVisualizer(
    editable=COMPONENT_SERVICES_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Gebruikte services",
    description="Selecteer welke services dit component gebruikt. Standaard zijn alle services geselecteerd.",
    help_text="Hiermee worden de juiste omgevingsvariabelen en netwerktoegang geconfigureerd.",
    attributes={"data-rerender": "true"},
)

COMPONENT_PATH_MATCH = EditableVisualizer(
    editable=COMPONENT_PATH_MATCH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Publicatie pad",
    description="Het pad waarop dit component gepubliceerd wordt.",
    help_text=(
        "Bijvoorbeeld: /, /api, /admin. "
        "Bij gedeelde domeinen wordt dit pad gebruikt om verkeer naar het juiste component te routeren. "
        "Gebruik de standaardwaarde als je niet zeker weet wat je moet invullen."
    ),
)

COMPONENT_PATH_REWRITE = EditableVisualizer(
    editable=COMPONENT_PATH_REWRITE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Rewrite pad",
    description="Optioneel. Meestal niet nodig. Gebruik de standaardwaarde tenzij je zeker weet wat je moet invullen.",
)

COMPONENT_PATH = EditableVisualizer(
    editable=COMPONENT_PATH_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Paden",
    children=[COMPONENT_PATH_MATCH, COMPONENT_PATH_REWRITE],
)

COMPONENT_ALIASES = EditableVisualizer(
    editable=COMPONENT_ALIASES_EDITABLE,
    widget=WidgetType.KEY_VALUE,
    label="Aliassen",
    description="Koppel platform-variabelen aan aangepaste namen voor dit component.",
    help_text=(
        "Wijs omgevingsvariabelen toe aan andere namen. "
        "Gebruik $VARIABELE_NAAM om platform-variabelen te refereren. "
        "Bijvoorbeeld: POSTGRES_HOST=$DATABASE_SERVER_HOST"
    ),
    attributes={"kv_format": "env"},
)

COMPONENT_USER_ENV_VARS = EditableVisualizer(
    editable=COMPONENT_USER_ENV_VARS_EDITABLE,
    widget=WidgetType.KEY_VALUE,
    label="Eigen omgevingsvariabelen",
    description="Voeg eigen omgevingsvariabelen toe voor dit component.",
    help_text=(
        "Definieer extra omgevingsvariabelen die beschikbaar worden in de container. "
        "Deze waarden worden versleuteld opgeslagen. "
        "Bijvoorbeeld: API_KEY=mijn-geheime-sleutel"
    ),
    attributes={"kv_format": "env"},
)

PERSISTENT_STORAGE_NAME = EditableVisualizer(
    editable=PERSISTENT_STORAGE_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    help_text="Unieke naam voor dit opslagvolume binnen het component.",
)

PERSISTENT_STORAGE_SIZE = EditableVisualizer(
    editable=PERSISTENT_STORAGE_SIZE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Grootte",
    help_text="De maximale grootte van het opslagvolume.",
)

PERSISTENT_STORAGE_MOUNT_PATH = EditableVisualizer(
    editable=PERSISTENT_STORAGE_MOUNT_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Het pad in de container waar het volume wordt gemount (bijv. /data, /var/lib/app).",
)

PERSISTENT_STORAGE_SEQUENCE = EditableVisualizer(
    editable=PERSISTENT_STORAGE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Persistente opslag",
    help_text="Persistente opslagvolumes die in de container worden gemount",
    children=[PERSISTENT_STORAGE_NAME, PERSISTENT_STORAGE_SIZE, PERSISTENT_STORAGE_MOUNT_PATH],
)

ATTACHMENT_USE_REFERENCE = EditableVisualizer(
    editable=ATTACHMENT_USE_REFERENCE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bijlage",
    help_text="Kies een geuploade bijlage uit de catalogus.",
)

ATTACHMENT_USE_PROVIDE_AS = EditableVisualizer(
    editable=ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Leveren als",
    help_text="Als bestand op een pad, of als waarde van een env-var (alleen tekstbestanden).",
    attributes={"data-rerender": "true"},
)

ATTACHMENT_USE_PATH = EditableVisualizer(
    editable=ATTACHMENT_USE_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mount pad",
    help_text="Vereist bij 'Als bestand': het pad in de container (bijv. /etc/tls/keystore.p12).",
)

ATTACHMENT_USE_ENV_NAME = EditableVisualizer(
    editable=ATTACHMENT_USE_ENV_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Env-var naam",
    help_text="Vereist bij 'Als env-var': de naam van de omgevingsvariabele (bijv. CA_BUNDLE).",
)

ATTACHMENT_USE_SEQUENCE = EditableVisualizer(
    editable=ATTACHMENT_USE_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Bijlagen",
    help_text="Koppel geuploade bijlagen aan dit component (als bestand of env-var)",
    children=[ATTACHMENT_USE_REFERENCE, ATTACHMENT_USE_PROVIDE_AS, ATTACHMENT_USE_PATH, ATTACHMENT_USE_ENV_NAME],
)

METRICS_PORT = EditableVisualizer(
    editable=METRICS_PORT_EDITABLE,
    widget=WidgetType.TEXT,
    label="Metrics poort",
    description="De poort waarop Prometheus metrics worden geserveerd.",
)

METRICS_PATH = EditableVisualizer(
    editable=METRICS_PATH_EDITABLE,
    widget=WidgetType.TEXT,
    label="Metrics pad",
    description="Het pad waarop de Prometheus metrics beschikbaar zijn.",
)

PUBLISH_ON_WEB_TLS = EditableVisualizer(
    editable=PUBLISH_ON_WEB_TLS_EDITABLE,
    widget=WidgetType.SELECT,
    label="TLS-modus",
    help_text=(
        "Standaard: het platform regelt het certificaat. Passthrough: de pod presenteert "
        "z'n eigen certificaat (koppel dat als bijlage aan dit component). Aangeleverd: eigen "
        "certificaat op de ingress (kies de PEM-bijlage). Let op: passthrough werkt alleen als "
        "dit component een eigen hostname heeft (een domain-format met het component erin), of "
        "als dit het enige gepubliceerde component is."
    ),
    attributes={"data-rerender": "true"},
)

PUBLISH_ON_WEB_ATTACHMENT = EditableVisualizer(
    editable=PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Certificaat (bijlage)",
    help_text="De PEM-bijlage (cert + key) die als certificaat op de ingress komt.",
)

COMPONENTS_SEQUENCE = EditableVisualizer(
    editable=COMPONENTS_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Componenten",
    children=[
        COMPONENT_NAME,
        COMPONENT_IMAGE,
        COMPONENT_RESOURCES_CPU_REQUEST,
        COMPONENT_RESOURCES_CPU_LIMIT,
        COMPONENT_RESOURCES_MEMORY_REQUEST,
        COMPONENT_RESOURCES_MEMORY_LIMIT,
        COMPONENT_PORTS_INBOUND,
        COMPONENT_PORTS_OUTBOUND,
        COMPONENT_SERVICES,
        COMPONENT_PATH,
        COMPONENT_ALIASES,
        COMPONENT_USER_ENV_VARS,
        PERSISTENT_STORAGE_SEQUENCE,
        TEMP_STORAGE_SEQUENCE,
        ATTACHMENT_USE_SEQUENCE,
        PUBLISH_ON_WEB_TLS,
        PUBLISH_ON_WEB_ATTACHMENT,
        METRICS_PORT,
        METRICS_PATH,
    ],
)
