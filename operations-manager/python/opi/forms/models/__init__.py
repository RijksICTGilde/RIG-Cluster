"""
Form models for OPI.

This package contains Pydantic models with FormMeta annotations
for dynamic form generation.
"""

from opi.forms.models.project import (
    ComponentFormModel,
    ProjectFormModel,
    UserFormModel,
    get_project_form_layout,
)
from opi.forms.models.project_file import (
    ComponentModel,
    DeploymentComponentModel,
    DeploymentModel,
    PortsModel,
    ProjectFileModel,
    ProjectUserModel,
    RepositoryModel,
    ResourcesModel,
    SecurityConfig,
    get_project_file_form_layout,
)

__all__ = [
    # Self-service form models
    "ComponentFormModel",
    "ComponentModel",
    "DeploymentComponentModel",
    "DeploymentModel",
    "PortsModel",
    # Project file models (matching YAML structure)
    "ProjectFileModel",
    "ProjectFormModel",
    "ProjectUserModel",
    "RepositoryModel",
    "ResourcesModel",
    "SecurityConfig",
    "UserFormModel",
    "get_project_file_form_layout",
    "get_project_form_layout",
]
