"""Pydantic models that map one-to-one onto the project file YAML, for form-based editing."""

from datetime import datetime
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
from opi.services.members import MemberKind
from opi.services.project_environment import Omgeving
from opi.utils.patterns import PROJECT_NAME_PYDANTIC


class DomainHistoryEntry(BaseModel):
    """Single audit trail entry for a domain/subdomain status change."""

    date: str
    status: str = Field(pattern=r"^(requested|approved|denied)$")
    by: str | None = None
    message: str | None = None


class AllowedDomainEntry(BaseModel):
    """A domain approved (or requested) for use in a project.

    Used for ALL non-default domains, platform and custom alike. The cluster default domain
    needs no entry.
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


class ApiTokenModel(BaseModel):
    """An issued API token of a member, as the project file holds it.

    Only the hash, never the value: after issuing, nobody reads the token back, the issuer
    included. Revoking is removing this block, so it is a commit with history.
    """

    name: str = Field(min_length=1, max_length=64)
    expires: datetime
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectUserModel(BaseModel):
    """A member of a project: a person, or a service account.

    A service account is not a second kind beside the members but a member with another kind
    of name: the same roles, the same tokens, only no human behind it. So one list says who
    belongs to this project, instead of two that drift apart.
    """

    type: MemberKind = Field(default=MemberKind.PERSON)

    email: Annotated[
        str | None,
        FormMeta(
            label="user.email",
            description="user.email.description",
            widget="text",
            placeholder="gebruiker@rijksoverheid.nl",
        ),
    ] = Field(default=None)

    #: Only on a service account, and then a DNS label: the name travels along in the actor
    #: and in the git trailer, so it has to be writable as a label everywhere.
    name: str | None = Field(default=None, pattern=r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")

    roles: Annotated[
        list[str],
        FormMeta(
            label="user.roles",
            description="user.roles.description",
            widget="multiselect",
            options_provider="UserRoleOptionsProvider",
        ),
    ] = Field(default_factory=lambda: ["developer"])

    tokens: list[ApiTokenModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_name_matches_kind(self) -> ProjectUserModel:
        """A person has an email address, a service account a name, never both.

        An email address on a service account would let it pass for a person with every
        reader that only looks at ``email``, which is the confusion
        :func:`opi.services.project_members.human_members` exists to prevent.
        """
        if self.type is MemberKind.SERVICE_ACCOUNT:
            if not self.name:
                raise ValueError("Een service account heeft een name nodig")
            if self.email:
                raise ValueError("Een service account heeft geen email")
        else:
            if not self.email:
                raise ValueError("Een lid heeft een email nodig")
            if self.name:
                raise ValueError("Alleen een service account heeft een name")
        return self


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

    A hidden YAML-only feature: not in the wizard or the detail-edit UI, so a user opts in by
    editing the project YAML. All three fields are optional and each defaults to 1001 inside
    the deployment template.

    A production (OpenShift) deployment IGNORES these overrides, because SCC admission injects
    a random UID per namespace; the template only emits the override when ``cluster`` is
    ``local`` or ``sandboxed-local``.
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

    description: Annotated[
        str | None,
        FormMeta(
            label="component.description",
            description="component.description.description",
            widget="textarea",
        ),
    ] = Field(default=None, max_length=500)

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

    # A hidden YAML-only feature: no FormMeta, so it stays out of the wizard and the
    # detail-edit UI and a user adds it by editing the YAML.
    security: SecurityConfig | None = Field(default=None)

    # A hidden YAML-only feature: override the container's entrypoint without a wrap image.
    # Maps straight onto K8s `containers[].command`, a list with no shell parsing. With both
    # set, the deployment-level override (DeploymentComponentModel.command) wins over this.
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

    environment: Annotated[
        Omgeving | None,
        FormMeta(
            label="deployment.environment",
            description="deployment.environment.description",
            widget="select",
            options_provider="EnvironmentOptionsProvider",
        ),
    ] = Field(default=None)

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


#: The rule: features/repository-pad.md; ``$defs/repository/path`` in ``project_v2.json``
#: carries the same pattern. Without a lookahead and with ``\z``, because pydantic validates
#: with the Rust regex engine.
REPOSITORY_PATH_PATTERN = (
    r"^(?:(?:[^./\\\u0000-\u001f]|\.[^.\\\u0000-\u001f])(?:[^.\\\u0000-\u001f]|\.[^.\\\u0000-\u001f])*)?\.?\z"
)


#: Only https: the link is clicked in a browser, where http, ssh or git@ gives nothing usable.
SOURCE_CODE_URL_PATTERN = r'^https://[^\s\u0000"<>]+\z'

SOURCE_CODE_URL_MAX_LENGTH = 500
SOURCE_CODE_NAME_MAX_LENGTH = 100
SOURCE_CODE_MAX_LINKS = 20


class SourceCodeLinkModel(BaseModel):
    """Informative link to the project's source code (url and optional name)."""

    url: Annotated[
        str,
        FormMeta(
            label="project.source_code.url",
            description="project.source_code.url.description",
            widget="text",
            placeholder="https://github.com/MinBZK/mijn-app",
        ),
    ] = Field(pattern=SOURCE_CODE_URL_PATTERN, max_length=SOURCE_CODE_URL_MAX_LENGTH)

    name: Annotated[
        str | None,
        FormMeta(
            label="project.source_code.name",
            description="project.source_code.name.description",
            widget="text",
            placeholder="Applicatie",
        ),
    ] = Field(default=None, min_length=1, max_length=SOURCE_CODE_NAME_MAX_LENGTH)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


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
    ] = Field(default=".", pattern=REPOSITORY_PATH_PATTERN)

    project_name: Annotated[
        str | None,
        FormMeta(
            label="repository.project_name",
            description="repository.project_name.description",
            widget="text",
        ),
    ] = Field(default=None)


class ProjectFileModel(BaseModel):
    """The full structure of a project file, as a ``zad-projects`` repository holds it."""

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
    ] = Field(min_length=1, pattern=PROJECT_NAME_PYDANTIC)

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

    source_code: Annotated[
        list[SourceCodeLinkModel],
        FormMeta(
            label="project.source_code",
            description="project.source_code.description",
            widget="sequence",
            section="basic",
        ),
    ] = Field(default_factory=list, alias="source-code", max_length=SOURCE_CODE_MAX_LINKS)

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

    # Domain restrictions, not editable through a form: an admin manages them.
    domains: DomainsModel | None = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_admin_user_exists(self) -> ProjectFileModel:
        """Ensure at least one HUMAN member has the admin role.

        Counting a service account would leave a project without a person who can still revoke
        a token or change a role, while its own token keeps working.
        """
        if not self.users:
            return self  # Empty users list is handled by min_items constraint

        admin_users = [u for u in self.users if "admin" in u.roles and u.type is not MemberKind.SERVICE_ACCOUNT]
        if not admin_users:
            raise ValueError("At least one user must have the 'admin' role")
        return self

    @model_validator(mode="after")
    def validate_source_code_urls_are_unique(self) -> ProjectFileModel:
        """Refuse the same url twice.

        ``uniqueItems`` on the schema field compares whole objects and so lets the same url
        through under another name; here and in ``UniqueSourceCodeUrlEnforcer`` only the url
        counts.
        """
        urls = [link.url for link in self.source_code]
        duplicates = sorted({url for url in urls if urls.count(url) > 1})
        if duplicates:
            raise ValueError(f"Dubbele broncodelinks: {', '.join(duplicates)}")
        return self


def get_project_file_form_layout() -> Fieldset:
    """The layout for editing a project file."""
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
