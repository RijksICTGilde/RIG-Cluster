"""Deployments section editables: deployment definition fields."""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable

DEPLOYMENT_NAME = ProjectEditable(
    yaml_path="deployments[*]/name",
    widget="text",
    label="Deployment naam",
    required=True,
    readonly_on_edit=True,
)

DEPLOYMENT_CLUSTER = ProjectEditable(
    yaml_path="deployments[*]/cluster",
    widget="select",
    label="Cluster",
    required=True,
    options_provider="ClusterOptionsProvider",
)

DEPLOYMENT_REPOSITORY = ProjectEditable(
    yaml_path="deployments[*]/repository",
    widget="select",
    label="Repository",
    options_provider="RepositoryOptionsProvider",
)

DEPLOYMENT_SUBDOMAIN = ProjectEditable(
    yaml_path="deployments[*]/subdomain",
    widget="text",
    label="Subdomein",
    description="Optioneel subdomein voor deze deployment",
)

DEPLOYMENT_BASE_DOMAIN = ProjectEditable(
    yaml_path="deployments[*]/base-domain",
    widget="select",
    label="Basisdomein",
    options_provider="BaseDomainOptionsProvider",
    help_text="Het basisdomein voor de URL's van deze deployment",
)

DEPLOYMENT_DOMAIN_MODE = ProjectEditable(
    yaml_path="deployments[*]/domain-mode",
    widget="select",
    label="Domeinmodus",
    options_provider="DomainModeOptionsProvider",
    help_text=(
        "Hoe URL's worden opgebouwd: 'component-specific' geeft elk component "
        "een eigen subdomein, 'nice-url' gebruikt pad-gebaseerde routing onder één domein"
    ),
)

DEPLOYMENT_CLONE_FROM = ProjectEditable(
    yaml_path="deployments[*]/clone-from",
    widget="select",
    label="Kloon data van",
    help_text=("Kopieer database- en opslagdata van een andere deployment. Handig voor preview/PR-omgevingen."),
)

# Deployment component sub-fields
DEPLOYMENT_COMP_REFERENCE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/reference",
    widget="select",
    label="Component",
    required=True,
    options_provider="ComponentReferenceOptionsProvider",
)

DEPLOYMENT_COMP_IMAGE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/image",
    widget="text",
    label="Container image",
    required=True,
)

DEPLOYMENT_COMP_PULL_POLICY = ProjectEditable(
    yaml_path="deployments[*]/components[*]/imagePullPolicy",
    widget="select",
    label="Pull policy",
    options_provider="PullPolicyOptionsProvider",
)

DEPLOYMENT_COMPONENTS_SEQ = ProjectEditable(
    yaml_path="deployments[*]/components",
    widget="sequence",
    label="Deployment componenten",
    min_items=1,
    children=[DEPLOYMENT_COMP_REFERENCE, DEPLOYMENT_COMP_IMAGE, DEPLOYMENT_COMP_PULL_POLICY],
)

DEPLOYMENTS_SEQUENCE = ProjectEditable(
    yaml_path="deployments",
    widget="sequence",
    label="Deployments",
    children=[
        DEPLOYMENT_NAME,
        DEPLOYMENT_CLUSTER,
        DEPLOYMENT_REPOSITORY,
        DEPLOYMENT_SUBDOMAIN,
        DEPLOYMENT_BASE_DOMAIN,
        DEPLOYMENT_DOMAIN_MODE,
        DEPLOYMENT_CLONE_FROM,
        DEPLOYMENT_COMPONENTS_SEQ,
    ],
)
