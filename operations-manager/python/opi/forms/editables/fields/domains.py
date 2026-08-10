"""The wizard's "Webadres" step: the deployment identity around it, and the group.

The seven domain fields themselves are NOT defined here any more (RC-60). They belong to
publish-on-web, live in ``opi/services/catalog/publish_on_web/editables.py`` and are
re-exported below under their familiar names, so flows, visualizers and tests keep one
place to import from while there is only one definition.

What stays here is what is not the service's: the deployment name the wizard asks for, and
only the deployment name the wizard asks for; ``DOMAIN_CONFIG_EDITABLE``, the enforced
group the step edits as one unit, moved to the service with its children.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import KubernetesNameValidator
from opi.services.catalog.publish_on_web.editables import (
    DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CONFIG_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_ISSUER_EDITABLE,
    DOMAIN_MODE_EDITABLE,
    DOMAIN_REQUEST_DOMAIN_EDITABLE,
    DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
)

__all__ = [
    "DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE",
    "DOMAIN_BASE_DOMAIN_EDITABLE",
    "DOMAIN_CONFIG_EDITABLE",
    "DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE",
    "DOMAIN_FORMAT_EDITABLE",
    "DOMAIN_ISSUER_EDITABLE",
    "DOMAIN_MODE_EDITABLE",
    "DOMAIN_REQUEST_DOMAIN_EDITABLE",
    "DOMAIN_REQUEST_SUBDOMAIN_EDITABLE",
    "DOMAIN_ROOT_COMPONENT_EDITABLE",
    "DOMAIN_SUBDOMAIN_EDITABLE",
    "WIZARD_DEPLOYMENT_NAME_EDITABLE",
]

WIZARD_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/name",
    required=True,
    default="productie",
    validator=KubernetesNameValidator("Deploymentnaam"),
)
