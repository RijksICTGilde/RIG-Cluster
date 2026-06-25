"""Deployments section editables: deployment definition fields."""

from __future__ import annotations

from opi.forms.editables.conditions import SentinelValueCondition
from opi.forms.editables.converters import (
    CloneFromConverter,
    CustomDomainSelectConverter,
    KeyValueConverter,
    RRuleDayConverter,
    RRuleFrequencyConverter,
    RRuleMonthDayConverter,
    RRuleTimeConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    BaseDomainValidator,
    CustomDomainValidator,
    DomainFormatValidator,
    KeyValueValidator,
    PathValidator,
    SubdomainValidator,
)

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
    yaml_path="deployments[*]/base-domain",
    values_provider="BaseDomainOptionsProvider",
    validator=BaseDomainValidator(),
    converter=CustomDomainSelectConverter(),
    defers_to="deployments[*]/base-domain:custom",
    defer_when=SentinelValueCondition(),
)
DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/base-domain:custom",
    transient=True,
    validator=CustomDomainValidator(),
)
DEPLOYMENT_DOMAIN_MODE_EDITABLE = Editable(
    yaml_path="deployments[*]/domain-mode", values_provider="DomainModeOptionsProvider"
)
DEPLOYMENT_DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path="deployments[*]/domain-format",
    values_provider="DomainFormatOptionsProvider",
    validator=DomainFormatValidator(),
)
DEPLOYMENT_CLONE_FROM_EDITABLE = Editable(
    yaml_path="deployments[*]/clone-from",
    values_provider="DeploymentCloneFromOptionsProvider",
    converter=CloneFromConverter(),
    remove_when_none=True,
)

DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE = Editable(
    yaml_path="deployments[*]/backup/schedule",
    values_provider="BackupScheduleFrequencyOptionsProvider",
    converter=RRuleFrequencyConverter(),
    remove_when_none=True,
)
DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE = Editable(
    yaml_path="deployments[*]/backup/schedule:time",
    transient=True,
    values_provider="BackupScheduleTimeOptionsProvider",
    converter=RRuleTimeConverter(),
    depends_on="deployments[*]/backup/schedule",
)
DEPLOYMENT_BACKUP_SCHEDULE_DAY_EDITABLE = Editable(
    yaml_path="deployments[*]/backup/schedule:day",
    transient=True,
    values_provider="BackupScheduleDayOptionsProvider",
    converter=RRuleDayConverter(),
    depends_on="deployments[*]/backup/schedule",
    show_when={"value": ["WEEKLY"]},
)
DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY_EDITABLE = Editable(
    yaml_path="deployments[*]/backup/schedule:monthday",
    transient=True,
    values_provider="BackupScheduleMonthDayOptionsProvider",
    converter=RRuleMonthDayConverter(),
    depends_on="deployments[*]/backup/schedule",
    show_when={"value": ["MONTHLY"]},
)
DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE = Editable(
    yaml_path="deployments[*]/backup/resource_types",
    values_provider="BackupResourceTypesOptionsProvider",
    depends_on="deployments[*]/backup/schedule",
)

# Manual backup editables (transient — not saved to YAML)
BACKUP_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployment_name",
    transient=True,
    values_provider="BackupDeploymentOptionsProvider",
)
BACKUP_RESOURCE_TYPES_EDITABLE = Editable(
    yaml_path="resource_types",
    transient=True,
    values_provider="BackupResourceTypesOptionsProvider",
)

DEPLOYMENT_COMP_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/reference",
    required=True,
    values_provider="ComponentReferenceOptionsProvider",
)
DEPLOYMENT_COMP_IMAGE_EDITABLE = Editable(yaml_path="deployments[*]/components[*]/image", required=True)
DEPLOYMENT_COMP_PULL_POLICY_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/imagePullPolicy", values_provider="PullPolicyOptionsProvider"
)
DEPLOYMENT_COMP_PATH_MATCH_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/path[*]/match",
    validator=PathValidator(),
)
DEPLOYMENT_COMP_PATH_REWRITE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/path[*]/rewrite",
    validator=PathValidator(),
    remove_when_none=True,
)
DEPLOYMENT_COMP_PATH_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/path",
    children=[
        DEPLOYMENT_COMP_PATH_MATCH_EDITABLE,
        DEPLOYMENT_COMP_PATH_REWRITE_EDITABLE,
    ],
)
DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/user-env-vars",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=KeyValueValidator(),
    remove_when_none=True,
)

# Per-deployment attachment coupling override. Mirrors the base-component coupling
# (components[*]/services{attachments}/config) but on the deployment component, so a
# certificate/file can differ per deployment. Optional (min_items=0): an empty list
# means "use whatever the base component couples". Merge semantics (deployment wins
# per reference) live in resolve_attachments_for_component.
DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/attachments/config[*]/reference",
    values_provider="AttachmentOptionsProvider",
    required=True,
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/attachments/config[*]/provide-as",
    values_provider="AttachmentProvideAsOptionsProvider",
    required=True,
    default="file",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PATH_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/attachments/config[*]/path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/attachments/config[*]/provide-as",
    show_when={"value": ["file"]},
)

DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/attachments/config[*]/env-name",
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/attachments/config[*]/provide-as",
    show_when={"value": ["env-var"]},
)

# Stored under ``attachments.config`` on the deployment component (NOT under
# ``services`` - that key is the deployment service-revision map with a different
# shape). The ``config`` wrapper matches the base-component coupling. No virtualize
# needed, so the sequence path is real end-to-end.
DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/attachments/config",
    min_items=0,
    remove_when_none=True,
    children=[
        DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
        DEPLOYMENT_COMP_ATTACHMENT_USE_PATH_EDITABLE,
        DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ],
)

DEPLOYMENT_COMPONENTS_SEQ_EDITABLE = Editable(
    yaml_path="deployments[*]/components",
    min_items=1,
    children=[
        DEPLOYMENT_COMP_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_IMAGE_EDITABLE,
        DEPLOYMENT_COMP_PULL_POLICY_EDITABLE,
        DEPLOYMENT_COMP_PATH_EDITABLE,
        DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE,
        DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE,
    ],
)

# Per-deployment publish-on-web TLS override. Empty value = "erven" (no override):
# resolution falls back to the component/root setting. Stored under a dedicated
# 'publish-on-web' key on the deployment component (its 'services' is the service-
# revision map). See features/publish-on-web-tls-modes.md.
DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/publish-on-web/config/tls",
    values_provider="PublishTlsOverrideOptionsProvider",
    remove_when_none=True,
)

DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/publish-on-web/config/attachment",
    values_provider="AttachmentOptionsProvider",
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/publish-on-web/config/tls",
    show_when={"value": ["provided"]},
)

# Focused, read-only-component sequence for the domain wizard's per-component TLS step.
# Reuses the deployment-components path; the reference is shown read-only (you only pick
# the TLS mode here, you do not add/remove components).
DEPLOYMENT_CERT_COMPONENTS_SEQ_EDITABLE = Editable(
    yaml_path="deployments[*]/components",
    min_items=0,
    add_remove=False,
    children=[
        DEPLOYMENT_COMP_REFERENCE_EDITABLE,
        DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE,
        DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE,
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
        DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE,
        DEPLOYMENT_DOMAIN_MODE_EDITABLE,
        DEPLOYMENT_DOMAIN_FORMAT_EDITABLE,
        DEPLOYMENT_CLONE_FROM_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_DAY_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY_EDITABLE,
        DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE,
        DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    ],
)
