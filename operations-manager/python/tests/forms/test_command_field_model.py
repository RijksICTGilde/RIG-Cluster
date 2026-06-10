"""Unit tests for the hidden per-component ``command`` override.

Covers the Pydantic field on both ``ComponentModel`` (component default) and
``DeploymentComponentModel`` (per-deployment override). The field is
optional; existing projects keep validating without modification.
"""

from __future__ import annotations

import pytest
from opi.forms.models.project_file import ComponentModel, DeploymentComponentModel
from pydantic import ValidationError


def test_component_without_command_defaults_to_none() -> None:
    """No ``command`` in YAML => attribute is ``None``."""
    component = ComponentModel.model_validate({"name": "web"})
    assert component.command is None


def test_component_accepts_command_list() -> None:
    """A non-empty list[str] is accepted on the component definition."""
    component = ComponentModel.model_validate(
        {
            "name": "web",
            "command": ["sh", "-c", "exec /app/bin/web"],
        }
    )

    assert component.command == ["sh", "-c", "exec /app/bin/web"]


def test_component_rejects_empty_command_list() -> None:
    """``min_length=1`` must reject ``command: []`` — an empty list would
    silently erase the image's ENTRYPOINT and surprise the user."""
    with pytest.raises(ValidationError):
        ComponentModel.model_validate({"name": "web", "command": []})


def test_deployment_component_without_command_defaults_to_none() -> None:
    """Override field is optional on the deployment-component reference."""
    dc = DeploymentComponentModel.model_validate({"reference": "web", "image": "nginx:1.27"})
    assert dc.command is None


def test_deployment_component_accepts_command_override() -> None:
    """Per-deployment override of the container command (wins over component default)."""
    dc = DeploymentComponentModel.model_validate(
        {
            "reference": "web",
            "image": "nginx:1.27",
            "command": ["sh", "-c", "echo prd && exec /app/bin/web"],
        }
    )

    assert dc.command == ["sh", "-c", "echo prd && exec /app/bin/web"]


def test_deployment_component_rejects_empty_command_list() -> None:
    """Same ``min_length=1`` guard applies to the deployment-level override."""
    with pytest.raises(ValidationError):
        DeploymentComponentModel.model_validate({"reference": "web", "image": "nginx:1.27", "command": []})


def test_component_rejects_non_string_items() -> None:
    """Items must be strings (K8s container command is list[str])."""
    with pytest.raises(ValidationError):
        ComponentModel.model_validate({"name": "web", "command": ["sh", 42]})
