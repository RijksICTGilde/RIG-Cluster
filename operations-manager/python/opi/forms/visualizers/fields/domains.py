"""Domains/wizard visualizer constants.

TODO: Many visualizers here duplicate those in deployments.py (DEPLOYMENT_*).
  The only difference is the YAML path: [0] (wizard, single deployment) vs [*]
  (deployment editor, sequence). Refactor to share UI definitions with a path adapter.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.domains import (
    DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CONFIG_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_REQUEST_DOMAIN_EDITABLE,
    DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    WIZARD_DEPLOYMENT_NAME_EDITABLE,
)
from opi.forms.visualizers.visualizer import EditableVisualizer

DOMAIN_FORMAT = EditableVisualizer(
    editable=DOMAIN_FORMAT_EDITABLE,
    widget=WidgetType.SELECT,
    label="URL-formaat",
    help_text="Bepaalt hoe URL's voor je componenten worden opgebouwd.",
    attributes={"data-rerender": "true"},
)

DOMAIN_SUBDOMAIN = EditableVisualizer(
    editable=DOMAIN_SUBDOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Subdomein",
    help_text="Het subdomein voor je applicatie-URLs.",
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
    help_text="Voer je eigen domeinnaam in. Je bent zelf verantwoordelijk voor DNS-configuratie. Gebruik het domein zonder 'subdomein', dus voorbeeld.nl en niet www.voorbeeld.nl",
    attributes={"data-rerender": "true"},
)

DOMAIN_ROOT_COMPONENT = EditableVisualizer(
    editable=DOMAIN_ROOT_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Root component",
    help_text="Optioneel. Het component dat ook bereikbaar wordt op de kortere basis-URL.",
    attributes={"data-rerender": "true"},
)

DOMAIN_BARE_DOMAIN_COMPONENT = EditableVisualizer(
    editable=DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
    widget=WidgetType.SELECT,
    label="Bereikbaar op kaal domein",
    help_text=(
        "Optioneel. Het component dat ook bereikbaar wordt op het kale domein "
        "(bijv. voorbeeld.nl naast www.voorbeeld.nl)."
    ),
    attributes={"data-rerender": "true"},
)

DOMAIN_REQUEST_DOMAIN = EditableVisualizer(
    editable=DOMAIN_REQUEST_DOMAIN_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Domein aanvragen",
    help_text=(
        "Het gekozen domein is nog niet goedgekeurd voor dit project. "
        "Vink de checkbox aan om gebruik van het domein aan te vragen. Na goedkeuring door een beheerder "
        "kan het domein worden gebruikt."
    ),
)

DOMAIN_REQUEST_SUBDOMAIN = EditableVisualizer(
    editable=DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Subdomein aanvragen",
    help_text=(
        "Het gekozen subdomein is nog niet goedgekeurd voor dit domein. "
        "Vink de checkbox aan om gebruik van het subdomein aan te vragen en verder te gaan. Na goedkeuring door een beheerder "
        "kan het subdomein worden gebruikt."
    ),
)

DOMAIN_CONFIG = EditableVisualizer(
    editable=DOMAIN_CONFIG_EDITABLE,
    widget=WidgetType.GROUP,
    label="Domeinconfiguratie",
    children=[
        DOMAIN_BASE_DOMAIN,
        DOMAIN_REQUEST_DOMAIN,
        DOMAIN_CUSTOM_BASE_DOMAIN,
        DOMAIN_FORMAT,
        DOMAIN_SUBDOMAIN,
        DOMAIN_REQUEST_SUBDOMAIN,
        DOMAIN_ROOT_COMPONENT,
        DOMAIN_BARE_DOMAIN_COMPONENT,
    ],
)

WIZARD_DEPLOYMENT_NAME = EditableVisualizer(
    editable=WIZARD_DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Deployment naam",
    description="Alleen kleine letters, cijfers en streepjes.",
)
