"""Visualizers for the publish-on-web service, per layer.

Component level: the TLS mode and the certificate attachment, consumed by the
component-form aggregation via ``config_component_visualizers()``.

Deployment level (RC-60): the "Webadres" fields. They moved here with their editables --
the display of a field belongs with the field, and the service that owns the value owns
both. ``forms/visualizers/fields/domains.py`` re-exports them under their familiar names.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.publish_on_web.editables import (
    DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CONFIG_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_REQUEST_DOMAIN_EDITABLE,
    DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    PUBLISH_ON_WEB_TLS_EDITABLE,
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


# --- the deployment layer: the "Webadres" fields --------------------------------------

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
