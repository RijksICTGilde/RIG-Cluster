"""
Project file form model matching actual project YAML structure.

This module defines Pydantic models that directly map to the project
file YAML structure used in rig-cluster-projects, enabling form-based
editing of project configurations.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opi.forms.layout import (
    ButtonGroup,
    Column,
    Fieldset,
    Row,
    Sequence,
    Submit,
)
from opi.forms.schema import FormMeta


class DomainHistoryEntry(BaseModel):
    """Single audit trail entry for a domain/subdomain status change."""

    date: str
    status: str = Field(pattern=r"^(requested|approved|denied)$")
    by: str | None = None
    message: str | None = None


class AllowedDomainEntry(BaseModel):
    """A domain approved (or requested) for use in a project.

    Used for ALL non-default domains — both platform and custom.
    The cluster default domain doesn't need an entry.
    """

    model_config = ConfigDict(populate_by_name=True)

    domain: str
    status: str = Field(pattern=r"^(requested|approved|denied)$")
    supports_dots: Annotated[bool, Field(alias="supports-dots")] = False
    issuer: str | None = None
    restricted_subdomains: Annotated[bool, Field(alias="restricted-subdomains")] = False
    history: list[DomainHistoryEntry] = Field(default_factory=list)


class AllowedSubdomainDetail(BaseModel):
    """Individual subdomain with approval status."""

    name: str
    status: str = Field(pattern=r"^(requested|approved|denied)$")
    history: list[DomainHistoryEntry] = Field(default_factory=list)


class AllowedSubdomainEntry(BaseModel):
    """Allowed subdomains for a specific platform domain."""

    domain: str
    subdomains: list[AllowedSubdomainDetail] = Field(default_factory=list)


class DomainsModel(BaseModel):
    """Project-level domain configuration: domain and subdomain approval."""

    model_config = ConfigDict(populate_by_name=True)

    allowed_domains: list[AllowedDomainEntry] = Field(default_factory=list, alias="allowed-domains")
    allowed_subdomains: list[AllowedSubdomainEntry] = Field(default_factory=list, alias="allowed-subdomains")


class ProjectUserModel(BaseModel):
    """User with email and role in a project."""

    email: Annotated[
        str,
        FormMeta(
            label="user.email",
            description="user.email.description",
            widget="text",
            placeholder="gebruiker@rijksoverheid.nl",
        ),
    ] = Field(min_length=1)

    role: Annotated[
        str,
        FormMeta(
            label="user.role",
            description="user.role.description",
            widget="select",
            options_provider="UserRoleOptionsProvider",
        ),
    ] = Field(default="developer")


class PortsModel(BaseModel):
    """Port configuration for a component."""

    inbound: Annotated[
        list[int],
        FormMeta(
            label="component.ports.inbound",
            description="component.ports.inbound.description",
            widget="text",
            placeholder="8000, 8080",
            converter="IntegerListConverter",
        ),
    ] = Field(default_factory=list)

    outbound: Annotated[
        list[int],
        FormMeta(
            label="component.ports.outbound",
            description="component.ports.outbound.description",
            widget="text",
            placeholder="80, 443",
            converter="IntegerListConverter",
        ),
    ] = Field(default_factory=list)


class ResourcesModel(BaseModel):
    """Resource limits for a component."""

    cpu: Annotated[
        str,
        FormMeta(
            label="component.cpu_limit",
            description="component.cpu_limit.description",
            widget="select",
            options_provider="CpuLimitOptionsProvider",
        ),
    ] = Field(default="1")

    memory: Annotated[
        str,
        FormMeta(
            label="component.memory_limit",
            description="component.memory_limit.description",
            widget="select",
            options_provider="MemoryOptionsProvider",
        ),
    ] = Field(default="256Mi")


class SecurityConfig(BaseModel):
    """Pod-level security context overrides for a component.

    Hidden YAML-only feature: not exposed in the OPI wizard or detail-edit
    UI. Users opt in by editing the project YAML directly. All three fields
    are optional; each defaults to 1001 inside the deployment template
    (preserving the previous hardcoded sandbox behaviour).

    Production (OpenShift) deployments IGNORE these overrides because SCC
    admission injects a random UID per namespace. The template only emits
    the override when ``cluster`` is ``local`` or ``sandboxed-local``.
    """

    model_config = ConfigDict(populate_by_name=True)

    run_as_user: int | None = Field(default=None, alias="run-as-user", ge=0)
    run_as_group: int | None = Field(default=None, alias="run-as-group", ge=0)
    fs_group: int | None = Field(default=None, alias="fs-group", ge=0)


class ComponentModel(BaseModel):
    """Component definition in a project file."""

    name: Annotated[
        str,
        FormMeta(
            label="component.name",
            description="component.name.description",
            widget="text",
            placeholder="component-1",
        ),
    ] = Field(min_length=1)

    type: Annotated[
        str,
        FormMeta(
            label="component.type",
            description="component.type.description",
            widget="select",
            options_provider="ComponentTypeOptionsProvider",
        ),
    ] = Field(default="single")

    ports: Annotated[
        PortsModel,
        FormMeta(
            label="component.ports",
            description="component.ports.description",
            widget="nested",
        ),
    ] = Field(default_factory=PortsModel)

    path: Annotated[
        str | list[dict[str, str]] | None,
        FormMeta(
            label="component.path",
            description="component.path.description",
            widget="text",
            placeholder="/",
        ),
    ] = Field(default=None)

    resources: Annotated[
        ResourcesModel,
        FormMeta(
            label="component.resources",
            description="component.resources.description",
            widget="nested",
        ),
    ] = Field(default_factory=ResourcesModel)

    services: Annotated[
        list[str],
        FormMeta(
            label="component.services",
            description="component.services.description",
            widget="checkbox-group",
            options_provider="ServiceOptionsProvider",
        ),
    ] = Field(default_factory=list)

    uses_components: Annotated[
        list[str],
        FormMeta(
            label="component.uses_components",
            description="component.uses_components.description",
            widget="text",
        ),
    ] = Field(default_factory=list, alias="uses-components")

    aliases: Annotated[
        dict[str, str] | None,
        FormMeta(
            label="component.aliases",
            description="component.aliases.description",
            widget="textarea",
            converter="KeyValueConverter",
        ),
    ] = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)

    user_env_vars: Annotated[
        str | None,
        FormMeta(
            label="component.env_vars",
            description="component.env_vars.description",
            widget="textarea",
            placeholder="KEY=value",
        ),
    ] = Field(default=None, alias="user-env-vars")

    # Hidden YAML-only feature — intentionally has no FormMeta so it does not
    # appear in the wizard / detail-edit UI. Users add it by editing YAML.
    security: SecurityConfig | None = Field(default=None)

    # Hidden YAML-only feature: override the container's entrypoint without
    # needing a wrap image. Maps directly to K8s `containers[].command`
    # (a list — no shell parsing). Deployment-level override (see
    # DeploymentComponentModel.command) wins over this default when both set.
    command: list[str] | None = Field(default=None, min_length=1)


class DeploymentComponentModel(BaseModel):
    """Component reference in a deployment."""

    reference: Annotated[
        str,
        FormMeta(
            label="deployment.component.reference",
            description="deployment.component.reference.description",
            widget="text",
        ),
    ]

    image: Annotated[
        str,
        FormMeta(
            label="deployment.component.image",
            description="deployment.component.image.description",
            widget="text",
            placeholder="nginx:latest",
        ),
    ]

    imagePullPolicy: Annotated[
        str,
        FormMeta(
            label="deployment.component.pull_policy",
            description="deployment.component.pull_policy.description",
            widget="select",
        ),
    ] = Field(default="Always")

    # Hidden YAML-only feature: per-deployment override of the container's
    # entrypoint. When set, wins over ComponentModel.command. When omitted,
    # falls back to the component-level command (if any) and finally to the
    # image's default ENTRYPOINT/CMD.
    command: list[str] | None = Field(default=None, min_length=1)


class DeploymentModel(BaseModel):
    """Deployment configuration for a cluster."""

    name: Annotated[
        str,
        FormMeta(
            label="deployment.name",
            description="deployment.name.description",
            widget="text",
            placeholder="deployment-1",
        ),
    ]

    cluster: Annotated[
        str,
        FormMeta(
            label="deployment.cluster",
            description="deployment.cluster.description",
            widget="select",
            options_provider="ClusterOptionsProvider",
        ),
    ]

    namespace: Annotated[
        str,
        FormMeta(
            label="deployment.namespace",
            description="deployment.namespace.description",
            widget="text",
        ),
    ]

    repository: Annotated[
        str,
        FormMeta(
            label="deployment.repository",
            description="deployment.repository.description",
            widget="text",
        ),
    ]

    subdomain: Annotated[
        str | None,
        FormMeta(
            label="deployment.subdomain",
            description="deployment.subdomain.description",
            widget="text",
        ),
    ] = Field(default=None)

    components: Annotated[
        list[DeploymentComponentModel],
        FormMeta(
            label="deployment.components",
            description="deployment.components.description",
            widget="sequence",
        ),
    ] = Field(default_factory=list)

    configuration: Annotated[
        str | None,
        FormMeta(
            label="deployment.configuration",
            description="deployment.configuration.description",
            widget="textarea",
        ),
    ] = Field(default=None)

    data_retention_period: Annotated[
        str | None,
        FormMeta(
            label="deployment.data_retention_period",
            description="deployment.data_retention_period.description",
            widget="text",
            placeholder="0h",
        ),
    ] = Field(default=None, alias="data-retention-period")


class RepositoryModel(BaseModel):
    """Git repository configuration."""

    name: Annotated[
        str,
        FormMeta(
            label="repository.name",
            description="repository.name.description",
            widget="text",
            placeholder="main-repo",
            readonly_on_edit=True,  # Repository name cannot be changed after creation
        ),
    ]

    url: Annotated[
        str,
        FormMeta(
            label="repository.url",
            description="repository.url.description",
            widget="text",
            placeholder="https://github.com/...",
            readonly_on_edit=True,  # Repository URL cannot be changed after creation
        ),
    ]

    username: Annotated[
        str,
        FormMeta(
            label="repository.username",
            description="repository.username.description",
            widget="text",
        ),
    ] = Field(default="git")

    password: Annotated[
        str | None,
        FormMeta(
            label="repository.password",
            description="repository.password.description",
            widget="password",
        ),
    ] = Field(default=None)

    branch: Annotated[
        str,
        FormMeta(
            label="repository.branch",
            description="repository.branch.description",
            widget="text",
        ),
    ] = Field(default="main")

    path: Annotated[
        str,
        FormMeta(
            label="repository.path",
            description="repository.path.description",
            widget="text",
        ),
    ] = Field(default=".")

    project_name: Annotated[
        str | None,
        FormMeta(
            label="repository.project_name",
            description="repository.project_name.description",
            widget="text",
        ),
    ] = Field(default=None)


class ProjectFileModel(BaseModel):
    """
    Complete project file model matching the YAML structure.

    This model represents the full structure of a project file as used
    in rig-cluster-projects repositories.
    """

    # Basic info
    name: Annotated[
        str,
        FormMeta(
            label="project.name",
            description="project.name.description",
            widget="text",
            placeholder="my-project",
            section="basic",
            readonly_on_edit=True,  # Project name cannot be changed after creation
        ),
    ] = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")

    display_name: Annotated[
        str,
        FormMeta(
            label="project.display_name",
            description="project.display_name.description",
            widget="text",
            placeholder="Mijn Project",
            section="basic",
        ),
    ] = Field(min_length=1, alias="display-name")

    description: Annotated[
        str | None,
        FormMeta(
            label="project.description",
            description="project.description.description",
            widget="textarea",
            section="basic",
        ),
    ] = Field(default=None)

    # Infrastructure
    clusters: Annotated[
        list[str],
        FormMeta(
            label="project.clusters",
            description="project.clusters.description",
            widget="checkbox-group",
            options_provider="ClusterOptionsProvider",
            section="infrastructure",
            min_items=1,  # At least one cluster required
        ),
    ] = Field(default_factory=list)

    services: Annotated[
        list[str | dict],
        FormMeta(
            label="project.services",
            description="project.services.description",
            widget="service-cards",
            options_provider="ServiceOptionsProvider",
            section="infrastructure",
        ),
    ] = Field(default_factory=list)

    # Team
    users: Annotated[
        list[ProjectUserModel],
        FormMeta(
            label="project.users",
            description="project.users.description",
            widget="sequence",
            section="team",
            min_items=1,  # At least one user required
        ),
    ] = Field(default_factory=list)

    # Source code
    repositories: Annotated[
        list[RepositoryModel],
        FormMeta(
            label="project.repositories",
            description="project.repositories.description",
            widget="sequence",
            section="source",
        ),
    ] = Field(default_factory=list)

    # Components
    components: Annotated[
        list[ComponentModel],
        FormMeta(
            label="project.components",
            description="project.components.description",
            widget="sequence",
            section="components",
            min_items=1,  # At least one component required
        ),
    ] = Field(default_factory=list)

    # Deployments
    deployments: Annotated[
        list[DeploymentModel],
        FormMeta(
            label="project.deployments",
            description="project.deployments.description",
            widget="sequence",
            section="deployments",
        ),
    ] = Field(default_factory=list)

    # Domain restrictions (not editable via forms — managed manually or by admin)
    domains: DomainsModel | None = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_admin_user_exists(self) -> ProjectFileModel:
        """Ensure at least one user has admin role."""
        if not self.users:
            return self  # Empty users list is handled by min_items constraint

        admin_users = [u for u in self.users if u.role == "admin"]
        if not admin_users:
            raise ValueError("At least one user must have the 'admin' role")
        return self

    @classmethod
    def from_yaml_dict(cls, data: dict) -> ProjectFileModel:
        """
        Create a ProjectFileModel from a parsed YAML dict.

        Handles the translation of YAML keys to model field names.
        """
        return cls.model_validate(data)

    def to_yaml_dict(self) -> dict:
        """
        Convert to a dict suitable for YAML serialization.

        Uses the alias names for YAML compatibility.
        """
        return self.model_dump(by_alias=True, exclude_none=True)


def get_project_file_form_layout() -> Fieldset:
    """
    Get the layout for editing a project file.

    Returns a hierarchical layout structure for the project file form.
    """
    return Fieldset(
        legend="project.edit.title",
        children=[
            # Basic Information
            Fieldset(
                legend="project.basic_info",
                css_class="basic-info",
                children=[
                    Row(
                        children=[
                            Column("name", width=4),
                            Column("display_name", width=8),
                        ]
                    ),
                    "description",
                ],
            ),
            # Infrastructure
            Fieldset(
                legend="project.infrastructure",
                css_class="infrastructure",
                children=[
                    "clusters",
                    "services",
                ],
            ),
            # Team Members
            Fieldset(
                legend="project.team",
                css_class="team",
                collapsible=True,
                children=[
                    Sequence(
                        field_name="users",
                        child_layout=Row(
                            children=[
                                Column("email", width=8),
                                Column("role", width=4),
                            ]
                        ),
                        min_items=1,
                        add_label="common.add_user",
                        remove_label="common.remove",
                    ),
                ],
            ),
            # Repositories
            Fieldset(
                legend="project.repositories",
                css_class="repositories",
                collapsible=True,
                children=[
                    Sequence(
                        field_name="repositories",
                        child_layout=Fieldset(
                            legend="repository.details",
                            children=[
                                Row(
                                    children=[
                                        Column("name", width=4),
                                        Column("url", width=8),
                                    ]
                                ),
                                Row(
                                    children=[
                                        Column("branch", width=4),
                                        Column("path", width=4),
                                        Column("username", width=4),
                                    ]
                                ),
                            ],
                        ),
                        min_items=0,
                        add_label="common.add_repository",
                        remove_label="common.remove",
                    ),
                ],
            ),
            # Components
            Fieldset(
                legend="project.components.title",
                css_class="components",
                collapsible=True,
                children=[
                    Sequence(
                        field_name="components",
                        child_layout=Fieldset(
                            legend="component.details",
                            children=[
                                Row(
                                    children=[
                                        Column("name", width=4),
                                        Column("type", width=4),
                                        Column("path", width=4),
                                    ]
                                ),
                                Row(
                                    children=[
                                        Column("resources.cpu", width=6),
                                        Column("resources.memory", width=6),
                                    ]
                                ),
                                "services",
                                "aliases",
                            ],
                        ),
                        min_items=0,
                        add_label="common.add_component",
                        remove_label="common.remove",
                    ),
                ],
            ),
            # Submit
            ButtonGroup(
                buttons=[
                    Submit(label="common.save", name="save", kind="primary"),
                    Submit(label="common.cancel", name="cancel", kind="secondary"),
                ],
                alignment="end",
            ),
        ],
    )
