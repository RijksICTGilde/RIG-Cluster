"""Deployments section editables: deployment definition fields."""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import BaseDomainValidator, DomainFormatValidator, SubdomainValidator

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

DEPLOYMENT_NAME_EDITABLE = Editable(yaml_path="deployments[*]/name", required=True)
DEPLOYMENT_CLUSTER_EDITABLE = Editable(
    yaml_path="deployments[*]/cluster", required=True, values_provider="ClusterOptionsProvider"
)
DEPLOYMENT_REPOSITORY_EDITABLE = Editable(
    yaml_path="deployments[*]/repository", values_provider="RepositoryOptionsProvider"
)
DEPLOYMENT_SUBDOMAIN_EDITABLE = Editable(yaml_path="deployments[*]/subdomain", validator=SubdomainValidator())
DEPLOYMENT_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/base-domain", values_provider="BaseDomainOptionsProvider", validator=BaseDomainValidator()
)
DEPLOYMENT_DOMAIN_MODE_EDITABLE = Editable(
    yaml_path="deployments[*]/domain-mode", values_provider="DomainModeOptionsProvider"
)
DEPLOYMENT_DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path="deployments[*]/domain-format",
    values_provider="DomainFormatOptionsProvider",
    depends_on="deployments[*]/domain-mode",
    validator=DomainFormatValidator(),
)
DEPLOYMENT_CLONE_FROM_EDITABLE = Editable(yaml_path="deployments[*]/clone-from")

DEPLOYMENT_COMP_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/reference",
    required=True,
    values_provider="ComponentReferenceOptionsProvider",
)
DEPLOYMENT_COMP_IMAGE_EDITABLE = Editable(yaml_path="deployments[*]/components[*]/image", required=True)
DEPLOYMENT_COMP_PULL_POLICY_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/imagePullPolicy", values_provider="PullPolicyOptionsProvider"
)
DEPLOYMENT_COMP_PATH_EDITABLE = Editable(yaml_path="deployments[*]/components[*]/paths")
DEPLOYMENT_COMP_REWRITE_PATH_EDITABLE = Editable(yaml_path="deployments[*]/components[*]/rewrite-path")

DEPLOYMENT_COMPONENTS_SEQ_EDITABLE = Editable(
    yaml_path="deployments[*]/components",
    min_items=1,
    children=[
        DEPLOYMENT_COMP_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_IMAGE_EDITABLE,
        DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
        DEPLOYMENT_COMP_PATH_EDITABLE,
        DEPLOYMENT_COMP_REWRITE_PATH_EDITABLE,
    ],
)

DEPLOYMENTS_SEQUENCE_EDITABLE = Editable(
    yaml_path="deployments",
    children=[
        DEPLOYMENT_NAME_EDITABLE,
        DEPLOYMENT_CLUSTER_EDITABLE,
        DEPLOYMENT_REPOSITORY_EDITABLE,
        DEPLOYMENT_SUBDOMAIN_EDITABLE,
        DEPLOYMENT_BASE_DOMAIN_EDITABLE,
        DEPLOYMENT_DOMAIN_MODE_EDITABLE,
        DEPLOYMENT_DOMAIN_FORMAT_EDITABLE,
        DEPLOYMENT_CLONE_FROM_EDITABLE,
        DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    ],
)
