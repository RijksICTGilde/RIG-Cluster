"""Visualizers for the cross-domain-access project-level form (RC-15).

Two SEQUENCE visualizers, ``inbound`` and ``outbound``, each with the six per-row fields.
The three peer fields cascade (project -> deployment -> component) and the port select
follows the RECEIVING side of the rule (RC-42). The port help text keeps the second pitfall
from the design (2.5): an authorization-wall makes the reachable port 4180, not the app port.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.cross_domain_access.editables import (
    DEPLOYMENT_INBOUND_NAME_EDITABLE,
    DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE,
    DEPLOYMENT_INBOUND_SEQUENCE_EDITABLE,
    DEPLOYMENT_OUTBOUND_NAME_EDITABLE,
    DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT_EDITABLE,
    DEPLOYMENT_OUTBOUND_SEQUENCE_EDITABLE,
    INBOUND_LOCAL_COMPONENT_EDITABLE,
    INBOUND_NAME_EDITABLE,
    INBOUND_PEER_COMPONENT_EDITABLE,
    INBOUND_PEER_DEPLOYMENT_EDITABLE,
    INBOUND_PEER_PROJECT_EDITABLE,
    INBOUND_PORT_EDITABLE,
    INBOUND_SEQUENCE_EDITABLE,
    OUTBOUND_LOCAL_COMPONENT_EDITABLE,
    OUTBOUND_NAME_EDITABLE,
    OUTBOUND_PEER_COMPONENT_EDITABLE,
    OUTBOUND_PEER_DEPLOYMENT_EDITABLE,
    OUTBOUND_PEER_PROJECT_EDITABLE,
    OUTBOUND_PORT_EDITABLE,
    OUTBOUND_SEQUENCE_EDITABLE,
)

_PORT_HELP = (
    "De poort van de ONTVANGENDE kant, en het is de containerpoort (niet de Service-poort). "
    "Staat er een authorization-wall voor het component, dan is 4180 de bereikbare poort."
)
_PEER_DEPLOYMENT_HELP = (
    "Welke deployment van dat project. Leeglaten kan: de regel geldt dan voor elke deployment "
    "en wordt per deployment ingevuld."
)
# Every field another field's option list depends on re-renders the step on change, so the
# next select in the cascade is filled the moment the previous one is answered.
_CASCADE = {"data-rerender": "true"}
_NAME_HELP = "Korte unieke naam voor deze regel; ook de sleutel waarop een deployment de regel kan aanpassen."

# inbound
INBOUND_NAME = EditableVisualizer(
    editable=INBOUND_NAME_EDITABLE, widget=WidgetType.TEXT, label="Naam", help_text=_NAME_HELP
)
INBOUND_PEER_PROJECT = EditableVisualizer(
    editable=INBOUND_PEER_PROJECT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bron-project",
    help_text="Het project dat binnen mag.",
    attributes=_CASCADE,
)
INBOUND_PEER_DEPLOYMENT = EditableVisualizer(
    editable=INBOUND_PEER_DEPLOYMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bron-deployment",
    help_text=_PEER_DEPLOYMENT_HELP,
    attributes=_CASCADE,
)
INBOUND_PEER_COMPONENT = EditableVisualizer(
    editable=INBOUND_PEER_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bron-component",
    help_text="Welk component van dat project. Volgt de gekozen deployment.",
)
INBOUND_LOCAL_COMPONENT = EditableVisualizer(
    editable=INBOUND_LOCAL_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Mijn component",
    help_text="Jouw component dat bereikt mag worden.",
    attributes=_CASCADE,
)
INBOUND_PORT = EditableVisualizer(
    editable=INBOUND_PORT_EDITABLE, widget=WidgetType.SELECT, label="Mijn poort", help_text=_PORT_HELP
)

INBOUND_SEQUENCE = EditableVisualizer(
    editable=INBOUND_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Inkomend",
    help_text="Wie mag bij mijn pods binnen. De ontvanger bepaalt wie toegang krijgt.",
    children=[
        INBOUND_NAME,
        INBOUND_PEER_PROJECT,
        INBOUND_PEER_DEPLOYMENT,
        INBOUND_PEER_COMPONENT,
        INBOUND_LOCAL_COMPONENT,
        INBOUND_PORT,
    ],
)

# outbound
OUTBOUND_NAME = EditableVisualizer(
    editable=OUTBOUND_NAME_EDITABLE, widget=WidgetType.TEXT, label="Naam", help_text=_NAME_HELP
)
OUTBOUND_LOCAL_COMPONENT = EditableVisualizer(
    editable=OUTBOUND_LOCAL_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Mijn component",
    help_text="Jouw component dat naar buiten mag.",
)
OUTBOUND_PEER_PROJECT = EditableVisualizer(
    editable=OUTBOUND_PEER_PROJECT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Doel-project",
    help_text="Het project waar je heen wilt.",
    attributes=_CASCADE,
)
OUTBOUND_PEER_DEPLOYMENT = EditableVisualizer(
    editable=OUTBOUND_PEER_DEPLOYMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Doel-deployment",
    help_text=_PEER_DEPLOYMENT_HELP,
    attributes=_CASCADE,
)
OUTBOUND_PEER_COMPONENT = EditableVisualizer(
    editable=OUTBOUND_PEER_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Doel-component",
    help_text="Welk component van dat project. Volgt de gekozen deployment.",
    attributes=_CASCADE,
)
OUTBOUND_PORT = EditableVisualizer(
    editable=OUTBOUND_PORT_EDITABLE, widget=WidgetType.SELECT, label="Doel-poort", help_text=_PORT_HELP
)

OUTBOUND_SEQUENCE = EditableVisualizer(
    editable=OUTBOUND_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Uitgaand",
    help_text="Waar mijn pods heen mogen. Nodig voor poorten buiten 80/443, en als expliciete intentie.",
    children=[
        OUTBOUND_NAME,
        OUTBOUND_LOCAL_COMPONENT,
        OUTBOUND_PEER_PROJECT,
        OUTBOUND_PEER_DEPLOYMENT,
        OUTBOUND_PEER_COMPONENT,
        OUTBOUND_PORT,
    ],
)

CROSS_DOMAIN_VISUALIZERS = [INBOUND_SEQUENCE, OUTBOUND_SEQUENCE]


# --- deployment layer (RC-42) ------------------------------------------------------------
# Deliberately two fields: the rule NAME (the key the patch merges on) and the peer
# deployment (the field a project rule may leave open). See the editables module for why the
# rest of the rule is not repeated here.

DEPLOYMENT_INBOUND_NAME = EditableVisualizer(
    editable=DEPLOYMENT_INBOUND_NAME_EDITABLE,
    widget=WidgetType.SELECT,
    label="Regel",
    help_text="De regel op projectniveau die deze deployment aanpast.",
    attributes=_CASCADE,
)
DEPLOYMENT_INBOUND_PEER_DEPLOYMENT = EditableVisualizer(
    editable=DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bron-deployment",
    help_text="De deployment van de ander die voor DEZE deployment geldt.",
)
DEPLOYMENT_INBOUND_SEQUENCE = EditableVisualizer(
    editable=DEPLOYMENT_INBOUND_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Inkomend",
    help_text="Per regel: welke deployment van de ander voor deze deployment geldt.",
    children=[DEPLOYMENT_INBOUND_NAME, DEPLOYMENT_INBOUND_PEER_DEPLOYMENT],
)

DEPLOYMENT_OUTBOUND_NAME = EditableVisualizer(
    editable=DEPLOYMENT_OUTBOUND_NAME_EDITABLE,
    widget=WidgetType.SELECT,
    label="Regel",
    help_text="De regel op projectniveau die deze deployment aanpast.",
    attributes=_CASCADE,
)
DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT = EditableVisualizer(
    editable=DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Doel-deployment",
    help_text="De deployment van de ander die voor DEZE deployment geldt.",
)
DEPLOYMENT_OUTBOUND_SEQUENCE = EditableVisualizer(
    editable=DEPLOYMENT_OUTBOUND_SEQUENCE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Uitgaand",
    help_text="Per regel: welke deployment van de ander voor deze deployment geldt.",
    children=[DEPLOYMENT_OUTBOUND_NAME, DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT],
)

CROSS_DOMAIN_DEPLOYMENT_VISUALIZERS = [DEPLOYMENT_INBOUND_SEQUENCE, DEPLOYMENT_OUTBOUND_SEQUENCE]
