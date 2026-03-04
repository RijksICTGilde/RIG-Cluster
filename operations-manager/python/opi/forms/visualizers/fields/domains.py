"""Domains/wizard visualizer constants."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.domains import (
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_MODE_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    WIZARD_DEPLOYMENT_NAME_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

DOMAIN_MODE = EditableVisualizer(
    editable=DOMAIN_MODE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Domeinmodus",
    help_text="Bepaalt hoe URL's voor uw componenten worden opgebouwd.",
    attributes={"data-rerender": "true"},
)

DOMAIN_SUBDOMAIN = EditableVisualizer(
    editable=DOMAIN_SUBDOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Subdomein",
    help_text="Het subdomein voor uw applicatie URLs.",
)

DOMAIN_BASE_DOMAIN = EditableVisualizer(
    editable=DOMAIN_BASE_DOMAIN_EDITABLE,
    widget=WidgetType.SELECT,
    label="Basisdomein",
    help_text="Het basisdomein voor nice-URL's. Afhankelijk van het gekozen cluster.",
)

DOMAIN_FORMAT = EditableVisualizer(
    editable=DOMAIN_FORMAT_EDITABLE,
    widget=WidgetType.SELECT,
    label="URL-formaat",
    help_text="Het patroon waarmee hostnamen worden opgebouwd.",
    attributes={"data-rerender": "true"},
)

DOMAIN_ROOT_COMPONENT = EditableVisualizer(
    editable=DOMAIN_ROOT_COMPONENT_EDITABLE,
    widget=WidgetType.RADIO,
    label="Root component",
    description="Het component dat bereikbaar is op de basis-URL (bijv. mijnapp.rijks.app)",
    help_text="Typisch uw frontend of single-page app. Maximaal 1 component.",
)

WIZARD_DEPLOYMENT_NAME = EditableVisualizer(
    editable=WIZARD_DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Deployment naam",
    description="Alleen kleine letters, cijfers en streepjes.",
)
