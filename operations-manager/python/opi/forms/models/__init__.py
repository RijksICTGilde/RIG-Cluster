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
    get_project_file_form_layout,
)

__all__ = [
    # Self-service form models
    "ComponentFormModel",
    "ProjectFormModel",
    "UserFormModel",
    "get_project_form_layout",
    # Project file models (matching YAML structure)
    "ProjectFileModel",
    "ProjectUserModel",
    "ComponentModel",
    "PortsModel",
    "ResourcesModel",
    "DeploymentModel",
    "DeploymentComponentModel",
    "RepositoryModel",
    "get_project_file_form_layout",
]
