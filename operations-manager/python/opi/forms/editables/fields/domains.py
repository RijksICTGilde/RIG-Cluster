"""Domain and deployment configuration editables for the create wizard.

These fields configure the initial deployment: domain/URL strategy and
deployment identity. All fields target ``deployments[0]/...`` paths since
the wizard creates a single deployment.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import MinMaxLengthValidator

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

DOMAIN_MODE_EDITABLE = Editable(
    yaml_path="deployments[0]/domain-mode",
    default="component-specific",
    values_provider="DomainModeOptionsProvider",
)

DOMAIN_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/subdomain",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": ["custom", "nice-url"]},
)

DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/base-domain",
    values_provider="ClusterBaseDomainOptionsProvider",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": "nice-url"},
)

DOMAIN_ROOT_COMPONENT_EDITABLE = Editable(
    yaml_path="deployments[0]/root-component",
    values_provider="ComponentReferenceOptionsProvider",
    depends_on="deployments[0]/domain-mode",
    show_when={"value": "nice-url"},
)

WIZARD_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[0]/name",
    required=True,
    default="productie",
    validator=MinMaxLengthValidator(2, 30),
)
