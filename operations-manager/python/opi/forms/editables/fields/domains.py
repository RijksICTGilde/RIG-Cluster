"""Domain and deployment configuration editables for the create wizard.

These fields configure the initial deployment: domain/URL strategy and
deployment identity. All fields target ``deployments[0]/...`` paths since
the wizard creates a single deployment.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    BaseDomainValidator,
    DomainFormatValidator,
    MinMaxLengthValidator,
    SubdomainValidator,
)

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path="deployments[0]/domain-format",
    required=True,
    default="component-deployment-project",
    values_provider="DomainFormatOptionsProvider",
    validator=DomainFormatValidator(),
)

DOMAIN_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/subdomain",
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["component-deployment-subdomain", "deployment-subdomain"]},
    validator=SubdomainValidator(),
)

DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/base-domain",
    required=True,
    values_provider="ClusterBaseDomainOptionsProvider",
    validator=BaseDomainValidator(),
)

DOMAIN_ROOT_COMPONENT_EDITABLE = Editable(
    yaml_path="deployments[0]/root-component",
    values_provider="ComponentReferenceOptionsProvider",
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["deployment-project", "deployment-subdomain"]},
)

WIZARD_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[0]/name",
    required=True,
    default="productie",
    validator=MinMaxLengthValidator(2, 30),
)
