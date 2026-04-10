"""Domain and deployment configuration editables for the create wizard.

These fields configure the initial deployment: domain/URL strategy and
deployment identity. All fields target ``deployments[*]/...`` paths since
the wizard creates a single deployment.
"""

from __future__ import annotations

from opi.forms.editables.conditions import (
    DomainNeedsRequestCondition,
    SentinelValueCondition,
    SubdomainNeedsRequestCondition,
)
from opi.forms.editables.converters import CustomDomainSelectConverter
from opi.forms.editables.editable import Editable, FormState
from opi.forms.editables.enforcers import DomainConfigEnforcer
from opi.forms.editables.generators import IssuerGenerator
from opi.forms.editables.hooks import DomainRequestHook, SubdomainRequestHook
from opi.forms.editables.resolvers import ClusterDefaultDomain
from opi.forms.editables.validators import (
    BaseDomainValidator,
    CustomDomainValidator,
    DomainFormatValidator,
    MinMaxLengthValidator,
    SubdomainValidator,
)
from opi.utils.naming import ROOT_COMPONENT_FORMAT_IDS, SUBDOMAIN_FORMAT_IDS

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path="deployments[*]/domain-format",
    required=True,
    default="component-deployment-project",
    values_provider="DomainFormatOptionsProvider",
    validator=DomainFormatValidator(),
)

DOMAIN_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/subdomain",
    required=True,
    depends_on="deployments[*]/domain-format",
    show_when={"value": SUBDOMAIN_FORMAT_IDS},
    validator=SubdomainValidator(),
)

DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/base-domain",
    values_provider="ClusterBaseDomainOptionsProvider",
    validator=BaseDomainValidator(),
    converter=CustomDomainSelectConverter(),
    defers_to="deployments[*]/base-domain:custom",
    defer_when=SentinelValueCondition(),
    remove_when_none=True,
    transient_value_when_none=ClusterDefaultDomain(),
)

DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/base-domain:custom",
    transient=True,
    depends_on="deployments[*]/base-domain",
    show_when={"value": ["__custom__"]},
    validator=CustomDomainValidator(),
)

DOMAIN_ROOT_COMPONENT_EDITABLE = Editable(
    yaml_path="deployments[*]/root-component",
    values_provider="RootComponentOptionsProvider",
    depends_on="deployments[*]/domain-format",
    show_when={"value": ROOT_COMPONENT_FORMAT_IDS},
)

DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE = Editable(
    yaml_path="deployments[*]/expose-component-on-bare-domain",
    values_provider="BareDomainComponentOptionsProvider",
    depends_on="deployments[*]/base-domain",
    show_when={"value": ["__custom__"]},
    remove_when_none=True,
)

DOMAIN_ISSUER_EDITABLE = Editable(
    yaml_path="deployments[*]/issuer",
    depends_on="deployments[*]/base-domain",
    generator=IssuerGenerator(),
    remove_when_none=True,
)

DOMAIN_REQUEST_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/_request-domain",
    transient=True,
    required=True,
    show_when=DomainNeedsRequestCondition(),
    hooks={
        FormState.PRE_SAVE: DomainRequestHook(),
    },
)

DOMAIN_REQUEST_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/_request-subdomain",
    transient=True,
    required=True,
    show_when=SubdomainNeedsRequestCondition(),
    hooks={
        FormState.PRE_SAVE: SubdomainRequestHook(),
    },
)

DOMAIN_CONFIG_EDITABLE = Editable(
    yaml_path="deployments[*]",
    enforcer=DomainConfigEnforcer(),
    children=[
        DOMAIN_FORMAT_EDITABLE,
        DOMAIN_BASE_DOMAIN_EDITABLE,
        DOMAIN_REQUEST_DOMAIN_EDITABLE,
        DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
        DOMAIN_SUBDOMAIN_EDITABLE,
        DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
        DOMAIN_ROOT_COMPONENT_EDITABLE,
        DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
        DOMAIN_ISSUER_EDITABLE,
    ],
)

WIZARD_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/name",
    required=True,
    default="productie",
    validator=MinMaxLengthValidator(2, 30),
)
