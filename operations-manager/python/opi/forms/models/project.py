"""
Project form model with FormMeta annotations.

This module defines the Pydantic models for project creation forms,
including user and component subforms, with FormMeta annotations
for dynamic form generation.
"""

from typing import Annotated

from pydantic import BaseModel, Field

from opi.forms.layout import (
    ButtonGroup,
    Column,
    Fieldset,
    Row,
    Sequence,
    Submit,
)
from opi.forms.schema import FormMeta


class UserFormModel(BaseModel):
    """
    User form model for project team members.

    Represents a single user with email and role.
    """

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


class ComponentFormModel(BaseModel):
    """
    Component form model for project components.

    Represents a deployable component with its configuration.
    """

    type: Annotated[
        str,
        FormMeta(
            label="component.type",
            description="component.type.description",
            widget="select",
            options_provider="ComponentTypeOptionsProvider",
        ),
    ] = Field(default="single")

    port: Annotated[
        int | None,
        FormMeta(
            label="component.port",
            description="component.port.description",
            widget="number",
            placeholder="8080",
        ),
    ] = Field(default=None, ge=1, le=65535)

    image: Annotated[
        str,
        FormMeta(
            label="component.image",
            description="component.image.description",
            widget="text",
            placeholder="nginx:latest",
        ),
    ] = Field(default="nginx:latest")

    path: Annotated[
        str,
        FormMeta(
            label="component.path",
            description="component.path.description",
            widget="text",
            placeholder="/",
        ),
    ] = Field(default="/")

    cpu_limit: Annotated[
        str | None,
        FormMeta(
            label="component.cpu_limit",
            description="component.cpu_limit.description",
            widget="select",
            options_provider="CpuLimitOptionsProvider",
        ),
    ] = Field(default=None)

    memory_limit: Annotated[
        str | None,
        FormMeta(
            label="component.memory_limit",
            description="component.memory_limit.description",
            widget="select",
            options_provider="MemoryOptionsProvider",
        ),
    ] = Field(default=None)

    env_vars: Annotated[
        str | None,
        FormMeta(
            label="component.env_vars",
            description="component.env_vars.description",
            widget="textarea",
            placeholder="KEY=value\nANOTHER_KEY=another_value",
            converter="KeyValueConverter",
        ),
    ] = Field(default=None)

    aliases: Annotated[
        str | None,
        FormMeta(
            label="component.aliases",
            description="component.aliases.description",
            widget="textarea",
            placeholder="DB_HOST=$POSTGRES_HOST",
        ),
    ] = Field(default=None)

    services: Annotated[
        list[str],
        FormMeta(
            label="project.services",
            description="project.services.description",
            widget="checkbox-group",
            options_provider="ServiceOptionsProvider",
        ),
    ] = Field(default_factory=list)


class ProjectFormModel(BaseModel):
    """
    Project form model for self-service project creation.

    This model defines all fields needed for creating a new project
    through the self-service portal.
    """

    # Project Details
    display_name: Annotated[
        str,
        FormMeta(
            label="project.display_name",
            description="project.display_name.description",
            widget="text",
            placeholder="Mijn Applicatie",
            section="details",
        ),
    ] = Field(min_length=3, max_length=100)

    description: Annotated[
        str | None,
        FormMeta(
            label="project.description",
            description="project.description.description",
            widget="textarea",
            placeholder="Beschrijf het doel van uw project...",
            section="details",
        ),
    ] = Field(default=None)

    cluster: Annotated[
        str,
        FormMeta(
            label="project.cluster",
            description="project.cluster.description",
            widget="select",
            options_provider="ClusterOptionsProvider",
            section="details",
        ),
    ] = Field(min_length=1)

    # Web Address Configuration
    domain_mode: Annotated[
        str,
        FormMeta(
            label="domain.mode",
            description="domain.mode.description",
            widget="radio",
            options_provider="DomainModeOptionsProvider",
            section="web",
        ),
    ] = Field(default="component-specific")

    subdomain: Annotated[
        str | None,
        FormMeta(
            label="domain.subdomain",
            description="domain.subdomain.description",
            widget="text",
            placeholder="mijn-app",
            section="web",
            depends_on="domain_mode",
            show_when={"domain_mode": "custom"},
        ),
    ] = Field(default=None)

    # Services
    services: Annotated[
        list[str],
        FormMeta(
            label="project.services",
            description="project.services.description",
            widget="checkbox-group",
            options_provider="ServiceOptionsProvider",
            section="services",
        ),
    ] = Field(default_factory=list)

    # Team Members (sequence)
    users: Annotated[
        list[UserFormModel],
        FormMeta(
            label="project.users",
            description="project.users.description",
            widget="sequence",
            section="users",
        ),
    ] = Field(default_factory=list)

    components: Annotated[
        list[ComponentFormModel],
        FormMeta(
            label="project.components",
            description="project.components.description",
            widget="sequence",
            section="components",
        ),
    ] = Field(default_factory=list)


def get_project_form_layout() -> Fieldset:
    """
    Get the layout for the project form.

    Returns a hierarchical layout structure defining how fields
    should be arranged in the form UI.
    """
    return Fieldset(
        legend="project.form.title",
        children=[
            # Project Details Section
            Fieldset(
                legend="project.details",
                css_class="project-details",
                children=[
                    Row(
                        children=[
                            Column("display_name", width=8),
                            Column("cluster", width=4),
                        ]
                    ),
                    "description",
                ],
            ),
            # Web Address Configuration
            Fieldset(
                legend="project.web_address",
                css_class="web-address",
                collapsible=True,
                children=[
                    "domain_mode",
                    "subdomain",
                ],
            ),
            # Services Section
            Fieldset(
                legend="project.services.title",
                css_class="services",
                children=[
                    "services",
                ],
            ),
            # Team Members Section
            Fieldset(
                legend="project.users.title",
                css_class="users",
                children=[
                    Sequence(
                        field_name="users",
                        child_layout=Row(
                            children=[
                                Column("email", width=8),
                                Column("role", width=4),
                            ]
                        ),
                        min_items=0,
                        add_label="common.add_user",
                        remove_label="common.remove",
                    ),
                ],
            ),
            # Components Section
            Fieldset(
                legend="project.components.title",
                css_class="components",
                children=[
                    Sequence(
                        field_name="components",
                        child_layout=Fieldset(
                            legend="component.details",
                            children=[
                                Row(
                                    children=[
                                        Column("type", width=4),
                                        Column("port", width=4),
                                        Column("path", width=4),
                                    ]
                                ),
                                "image",
                                Row(
                                    children=[
                                        Column("cpu_limit", width=6),
                                        Column("memory_limit", width=6),
                                    ]
                                ),
                                "env_vars",
                                "aliases",
                                "services",
                            ],
                        ),
                        min_items=1,
                        add_label="common.add_component",
                        remove_label="common.remove",
                    ),
                ],
            ),
            # Submit Button
            ButtonGroup(
                buttons=[
                    Submit(
                        label="common.submit",
                        name="submit",
                        kind="primary",
                    ),
                ],
                alignment="end",
            ),
        ],
    )
