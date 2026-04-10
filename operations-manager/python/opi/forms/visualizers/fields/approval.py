"""Visualizers for admin domain/subdomain approval flow."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.approval import (
    APPROVAL_ITEM_CURRENT_STATUS_EDITABLE,
    APPROVAL_ITEM_DOMAIN_EDITABLE,
    APPROVAL_ITEM_MESSAGE_EDITABLE,
    APPROVAL_ITEM_NAME_EDITABLE,
    APPROVAL_ITEM_STATUS_EDITABLE,
    APPROVAL_ITEM_TYPE_EDITABLE,
    APPROVAL_ITEMS_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

# Context fields — readonly display
APPROVAL_ITEM_TYPE = EditableVisualizer(
    editable=APPROVAL_ITEM_TYPE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Type",
    readonly=True,
)

APPROVAL_ITEM_DOMAIN = EditableVisualizer(
    editable=APPROVAL_ITEM_DOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Domein",
    readonly=True,
)

APPROVAL_ITEM_NAME = EditableVisualizer(
    editable=APPROVAL_ITEM_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    readonly=True,
)

APPROVAL_ITEM_CURRENT_STATUS = EditableVisualizer(
    editable=APPROVAL_ITEM_CURRENT_STATUS_EDITABLE,
    widget=WidgetType.TEXT,
    label="Huidige status",
    readonly=True,
)

# Action fields — editable
APPROVAL_ITEM_STATUS = EditableVisualizer(
    editable=APPROVAL_ITEM_STATUS_EDITABLE,
    widget=WidgetType.SELECT,
    label="Actie",
)

APPROVAL_ITEM_MESSAGE = EditableVisualizer(
    editable=APPROVAL_ITEM_MESSAGE_EDITABLE,
    widget=WidgetType.TEXT,
    label="Opmerking",
    placeholder="Optioneel: reden voor goedkeuring of afwijzing",
)

APPROVAL_ITEMS_SEQUENCE = EditableVisualizer(
    editable=APPROVAL_ITEMS_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Domein- en subdomeinaanvragen",
    children=[
        APPROVAL_ITEM_TYPE,
        APPROVAL_ITEM_DOMAIN,
        APPROVAL_ITEM_NAME,
        APPROVAL_ITEM_CURRENT_STATUS,
        APPROVAL_ITEM_STATUS,
        APPROVAL_ITEM_MESSAGE,
    ],
)
