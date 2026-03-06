"""Domains/wizard visualizer constants.

TODO: Many visualizers here duplicate those in deployments.py (DEPLOYMENT_*).
  The only difference is the YAML path: [0] (wizard, single deployment) vs [*]
  (deployment editor, sequence). Refactor to share UI definitions with a path adapter.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.domains import (
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CONFIG_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    WIZARD_DEPLOYMENT_NAME_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

DOMAIN_FORMAT = EditableVisualizer(
    editable=DOMAIN_FORMAT_EDITABLE,
    widget=WidgetType.SELECT,
    label="URL-formaat",
    help_text="Bepaalt hoe URL's voor uw componenten worden opgebouwd.",
    attributes={"data-rerender": "true"},
)

DOMAIN_SUBDOMAIN = EditableVisualizer(
    editable=DOMAIN_SUBDOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Subdomein",
    help_text="Het subdomein voor uw applicatie URLs.",
    attributes={"data-rerender": "true"},
)

DOMAIN_BASE_DOMAIN = EditableVisualizer(
    editable=DOMAIN_BASE_DOMAIN_EDITABLE,
    widget=WidgetType.SELECT,
    label="Basisdomein",
    help_text="Het basisdomein voor de URLs. Afhankelijk van het gekozen cluster.",
    attributes={"data-rerender": "true"},
)

DOMAIN_CUSTOM_BASE_DOMAIN = EditableVisualizer(
    editable=DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Eigen domein",
    placeholder="voorbeeld.nl",
    help_text="Voer uw eigen domeinnaam in. U bent zelf verantwoordelijk voor DNS-configuratie. Gebruik het domein zonder 'subdomein', dus voorbeeld.nl en niet www.voorbeeld.nl",
)

DOMAIN_ROOT_COMPONENT = EditableVisualizer(
    editable=DOMAIN_ROOT_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Root component",
    help_text="Optioneel. Het component dat ook bereikbaar wordt op de kortere basis-URL.",
)

DOMAIN_CONFIG = EditableVisualizer(
    editable=DOMAIN_CONFIG_EDITABLE,
    widget=WidgetType.GROUP,
    label="Domeinconfiguratie",
    children=[
        DOMAIN_BASE_DOMAIN,
        DOMAIN_CUSTOM_BASE_DOMAIN,
        DOMAIN_FORMAT,
        DOMAIN_SUBDOMAIN,
        DOMAIN_ROOT_COMPONENT,
    ],
)

WIZARD_DEPLOYMENT_NAME = EditableVisualizer(
    editable=WIZARD_DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Deployment naam",
    description="Alleen kleine letters, cijfers en streepjes.",
)
