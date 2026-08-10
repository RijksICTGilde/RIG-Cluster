"""The wizard's deployment-name visualizer, and the "Webadres" ones re-exported.

The domain visualizers moved to ``opi/services/catalog/publish_on_web/visualizers.py``
with the editables they display (RC-60): the service that owns a value owns how it is
shown. They are re-exported here so flows, sections and tests keep one import path.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.domains import WIZARD_DEPLOYMENT_NAME_EDITABLE
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.publish_on_web.visualizers import (
    DOMAIN_BARE_DOMAIN_COMPONENT,
    DOMAIN_BASE_DOMAIN,
    DOMAIN_CONFIG,
    DOMAIN_CUSTOM_BASE_DOMAIN,
    DOMAIN_FORMAT,
    DOMAIN_REQUEST_DOMAIN,
    DOMAIN_REQUEST_SUBDOMAIN,
    DOMAIN_ROOT_COMPONENT,
    DOMAIN_SUBDOMAIN,
)

__all__ = [
    "DOMAIN_BARE_DOMAIN_COMPONENT",
    "DOMAIN_BASE_DOMAIN",
    "DOMAIN_CONFIG",
    "DOMAIN_CUSTOM_BASE_DOMAIN",
    "DOMAIN_FORMAT",
    "DOMAIN_REQUEST_DOMAIN",
    "DOMAIN_REQUEST_SUBDOMAIN",
    "DOMAIN_ROOT_COMPONENT",
    "DOMAIN_SUBDOMAIN",
    "WIZARD_DEPLOYMENT_NAME",
]

WIZARD_DEPLOYMENT_NAME = EditableVisualizer(
    editable=WIZARD_DEPLOYMENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Deployment naam",
    description="Alleen kleine letters, cijfers en streepjes.",
)
