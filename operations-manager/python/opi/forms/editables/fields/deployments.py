"""Deployments section editables: deployment definition fields."""

from __future__ import annotations

from opi.forms.editables.converters import (
    CloneFromConverter,
    RRuleDayConverter,
    RRuleFrequencyConverter,
    RRuleMonthDayConverter,
    RRuleTimeConverter,
)
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import (
    KubernetesNameValidator,
    PathValidator,
)
from opi.services.catalog.publish_on_web.editables import (
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_MODE_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
)
from opi.services.registry import deployment_component_service_editables, deployment_service_editables

# ===========================================================================
# Pure Editable definitions (data logic only)
# ===========================================================================

DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/name", required=True, validator=KubernetesNameValidator("Deploymentnaam")
)
DEPLOYMENT_CLUSTER_EDITABLE = Editable(
    yaml_path="deployments[*]/cluster", required=True, values_provider="ClusterOptionsProvider"
)
DEPLOYMENT_REPOSITORY_EDITABLE = Editable(
    yaml_path="deployments[*]/repository", values_provider="RepositoryOptionsProvider"
)
# The web-address fields of a deployment are publish-on-web's (RC-60). This module used to
# define its OWN base-domain / subdomain / domain-mode / domain-format editables, with
# different providers and validators than the wizard's, for exactly the same yaml_path. Two
# definitions for one path in two flows is how a conversion ends up half-done: whoever moves
# one and misses the other leaves a flow writing the old shape. There is one set now, owned
# by the service, and the deployment sequence uses it under the familiar names.
DEPLOYMENT_SUBDOMAIN_EDITABLE = DOMAIN_SUBDOMAIN_EDITABLE
DEPLOYMENT_BASE_DOMAIN_EDITABLE = DOMAIN_BASE_DOMAIN_EDITABLE
DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE = DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE
DEPLOYMENT_DOMAIN_MODE_EDITABLE = DOMAIN_MODE_EDITABLE
DEPLOYMENT_DOMAIN_FORMAT_EDITABLE = DOMAIN_FORMAT_EDITABLE
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
# Per-deployment attachment coupling override. Mirrors the base-component coupling
# (components[*]/services{attachments}/config) but on the deployment component, so a
# certificate/file can differ per deployment. Optional (min_items=0): an empty list
# means "use whatever the base component couples". Merge semantics (deployment wins
# per reference) live in resolve_attachments_for_component.
DEPLOYMENT_COMP_ATTACHMENT_USE_REFERENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/attachments/config[*]/reference",
    values_provider="AttachmentOptionsProvider",
    required=True,
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PROVIDE_AS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/attachments/config[*]/provide-as",
    values_provider="AttachmentProvideAsOptionsProvider",
    required=True,
    default="file",
)

DEPLOYMENT_COMP_ATTACHMENT_USE_PATH_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/attachments/config[*]/path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/services/attachments/config[*]/provide-as",
    show_when={"value": ["file"]},
)

DEPLOYMENT_COMP_ATTACHMENT_USE_ENV_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/attachments/config[*]/env-name",
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/services/attachments/config[*]/provide-as",
    show_when={"value": ["env-var"]},
)

# Stored under ``services.attachments.config`` on the deployment component. The
# deployment ``services`` is a map (keyed by service name); ``attachments`` sits next
# to the system revision-map entries (persistent-storage etc.) and the ``config``
# wrapper matches the base-component coupling. No virtualize needed (services is a real
# map here), so the path is real end-to-end.
DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/attachments/config",
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
        # Per-service deployment-component fields, gathered from the registry (RC-25):
        # user-env-vars owns this layer's one field.
        *deployment_component_service_editables(),
        DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE,
    ],
)

# Per-deployment publish-on-web TLS override. Empty value = "erven" (no override):
# resolution falls back to the component/root setting. Stored under
# ``services.publish-on-web.config`` on the deployment component (services is a map;
# this sits next to the system revision-map entries). See
# features/publish-on-web-tls-modes.md.
DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/publish-on-web/config/tls",
    values_provider="PublishTlsOverrideOptionsProvider",
    remove_when_none=True,
)

DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/services/publish-on-web/config/attachment",
    values_provider="AttachmentOptionsProvider",
    remove_when_none=True,
    depends_on="deployments[*]/components[*]/services/publish-on-web/config/tls",
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
        # Per-service deployment fields, gathered from the registry (RC-60): the
        # web-address set is publish-on-web's, and nothing here names it.
        *deployment_service_editables(),
        DEPLOYMENT_CLONE_FROM_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_DAY_EDITABLE,
        DEPLOYMENT_BACKUP_SCHEDULE_MONTHDAY_EDITABLE,
        DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE,
        DEPLOYMENT_COMPONENTS_SEQ_EDITABLE,
    ],
)
