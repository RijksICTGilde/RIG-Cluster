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
    DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE,
    DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE,
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
)
from opi.services.registry import deployment_component_service_editables, deployment_service_editables

DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployments[*]/name", required=True, validator=KubernetesNameValidator("Deploymentnaam")
)
DEPLOYMENT_CLUSTER_EDITABLE = Editable(
    yaml_path="deployments[*]/cluster", required=True, values_provider="ClusterOptionsProvider"
)
# Empty means the default, development. remove_when_none then drops the key, so no deployment
# is left behind with an empty marking.
DEPLOYMENT_ENVIRONMENT_EDITABLE = Editable(
    yaml_path="deployments[*]/environment",
    values_provider="EnvironmentOptionsProvider",
    remove_when_none=True,
)
DEPLOYMENT_REPOSITORY_EDITABLE = Editable(
    yaml_path="deployments[*]/repository", values_provider="RepositoryOptionsProvider"
)
# The web-address fields of a deployment belong to publish-on-web and are re-exported here
# under the names the deployment sequence knows them by. Do not define a second set for these
# yaml_paths: two definitions for one path in two flows let one flow keep writing the old
# shape.
DEPLOYMENT_SUBDOMAIN_EDITABLE = DOMAIN_SUBDOMAIN_EDITABLE
DEPLOYMENT_BASE_DOMAIN_EDITABLE = DOMAIN_BASE_DOMAIN_EDITABLE
DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE = DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE
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

# Manual backup editables: transient, not saved to YAML.
BACKUP_DEPLOYMENT_NAME_EDITABLE = Editable(
    yaml_path="deployment_name",
    transient=True,
    values_provider="BackupDeploymentOptionsProvider",
)
# The deployment whose config a restore into a NEW deployment takes over. A transient field
# of its own and not ``clone-from``: after a restore that key carries the backup run, so the
# two cannot hold the same value.
RESTORE_CONFIG_SOURCE_EDITABLE = Editable(
    yaml_path="config_source",
    transient=True,
    values_provider="RestoreConfigSourceOptionsProvider",
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
# Per-deployment attachment coupling override, mirroring the base-component coupling
# (components[*]/services{attachments}/config) so a certificate or file can differ per
# deployment. An empty list means "use whatever the base component couples"; the merge, where
# the deployment wins per reference, lives in resolve_attachments_for_component.
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
        # Per-service deployment-component fields, gathered from the registry; user-env-vars
        # owns this layer's one field.
        *deployment_component_service_editables(),
        DEPLOYMENT_COMP_ATTACHMENT_USE_SEQUENCE_EDITABLE,
    ],
)

# Focused, read-only-component sequence for the domain wizard's per-component TLS step. It
# reuses the deployment-components path and shows the reference read-only: here you pick only
# the TLS mode, you do not add or remove components. The two override fields are
# publish-on-web's, imported from the service package rather than defined a second time, so
# the deployment form can gather them from the registry too.
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
        DEPLOYMENT_ENVIRONMENT_EDITABLE,
        DEPLOYMENT_REPOSITORY_EDITABLE,
        # Per-service deployment fields, gathered from the registry; the web-address set is
        # publish-on-web's, and nothing here names it.
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
