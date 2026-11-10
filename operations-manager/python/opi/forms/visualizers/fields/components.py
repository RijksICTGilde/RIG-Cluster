"""Components visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.components import (
    COMPONENT_COMMAND_EDITABLE,
    COMPONENT_DESCRIPTION_EDITABLE,
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
    COMPONENTS_SEQUENCE_EDITABLE,
    INBOUND_PORT_EDITABLE,
    OUTBOUND_PORT_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

# A service contributes its component visualizers through config_component_visualizers(),
# gathered here in config_component_order, so the tail of COMPONENTS_SEQUENCE is not a
# hand-synced list.
from opi.services.registry import component_service_notices, component_service_visualizers

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

COMPONENT_DESCRIPTION = EditableVisualizer(
    editable=COMPONENT_DESCRIPTION_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Omschrijving",
    help_text="Wat dit component doet, in een of twee zinnen. Optioneel.",
)

COMPONENT_IMAGE = EditableVisualizer(
    editable=COMPONENT_IMAGE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Container image",
    description="Docker image van je applicatie. Moet een rootless image zijn.",
    # One line, and short: together with the description above it this is all the text
    # between the label and the input. It may not be empty either, because
    # widgets/text.html.j2 only shows the description beside the question mark when there is
    # a help_text too, and otherwise a lone icon hangs above the field.
    help_text="Bijvoorbeeld: ghcr.io/minbzk/base-images/hello-world:latest",
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

# No placeholder with an example in it: a double quote comes back as &quot; in the rendered
# HTML, so the example would show exactly what not to type.
COMPONENT_COMMAND = EditableVisualizer(
    editable=COMPONENT_COMMAND_EDITABLE,
    widget=WidgetType.TEXT,
    label="Startcommando",
    help_text="Vervangt het commando uit het image. Laat leeg als je het niet zeker weet.",
)

COMPONENTS_SEQUENCE = EditableVisualizer(
    editable=COMPONENTS_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Componenten",
    children=[
        COMPONENT_NAME,
        COMPONENT_DESCRIPTION,
        COMPONENT_IMAGE,
        COMPONENT_COMMAND,
        COMPONENT_RESOURCES_CPU_REQUEST,
        COMPONENT_RESOURCES_CPU_LIMIT,
        COMPONENT_RESOURCES_MEMORY_REQUEST,
        COMPONENT_RESOURCES_MEMORY_LIMIT,
        COMPONENT_PORTS_INBOUND,
        COMPONENT_PORTS_OUTBOUND,
        COMPONENT_SERVICES,
        COMPONENT_PATH,
        # Per-service component visualizers, gathered from the registry in
        # config_component_order; the aliases and user-env-vars system services sort first.
        *component_service_visualizers(),
        # And what the services have to SAY at this level without configuring anything
        # (Service.component_form_notices). Readonly, so the processing skips them.
        *component_service_notices(),
    ],
)
