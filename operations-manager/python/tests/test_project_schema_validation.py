"""Red-green tests for the project schema validation chokepoint.

The Operations Manager must reject hostile project definitions before any
processing. These tests prove that:

- a hostile project (namespace with a newline, name with path traversal)
  is REJECTED by validate_project_schema, and
- a known-good example project from projects/ PASSES.
"""

from pathlib import Path

import pytest
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PROJECT = REPO_ROOT / "projects" / "simple-example.yaml"


def _valid_project() -> dict:
    return {
        "name": "valid-project",
        "description": "A valid project",
        "clusters": ["local", "odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "repositories": [
            {
                "name": "main-repo",
                "url": "ssh://git@host.docker.internal:2222/srv/git/valid.git",
                "branch": "main",
                "path": "infra",
            }
        ],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "ports": {"inbound": [8080], "outbound": [443]},
                "storage": [{"type": "persistent", "size": "10Gi", "mount-path": "/data"}],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "valid-project",
                "repository": "main-repo",
                "components": [{"reference": "frontend", "image": "nginx:latest"}],
            }
        ],
    }


def test_valid_project_passes() -> None:
    """A well-formed project must pass validation without raising."""
    validate_project_schema(_valid_project())


def test_deployment_with_scheduled_backup_passes() -> None:
    """Per-deployment backup config is a real production feature.

    The backup scheduler reads deployments[].backup.schedule (an RRULE) and
    the detail-edit form writes schedule:time/day/monthday keys into the same
    map. A fail-closed schema that omits this rejects every project using
    scheduled backups, which would stop legitimate deployments.
    """
    project = _valid_project()
    project["deployments"][0]["backup"] = {
        "enabled": True,
        "schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0",
        "resource_types": ["pvc", "database"],
        "schedule:time": "02:00",
        "schedule:day": "MO",
        "schedule:monthday": "1",
    }

    validate_project_schema(project)


def test_namespace_with_newline_is_rejected() -> None:
    """A namespace containing a newline (injection vector) must be rejected."""
    project = _valid_project()
    project["deployments"][0]["namespace"] = "valid-project\nrig-system"

    with pytest.raises(ProjectSchemaError) as exc:
        validate_project_schema(project)

    assert "namespace" in str(exc.value)


def test_project_name_with_path_traversal_is_rejected() -> None:
    """A project name containing ../ (path traversal) must be rejected."""
    project = _valid_project()
    project["name"] = "../../etc/passwd"

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_hostile_role_is_rejected() -> None:
    """A user role outside the allowed enum must be rejected."""
    project = _valid_project()
    project["users"][0]["role"] = "superadmin"

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_unknown_top_level_field_is_rejected() -> None:
    """An unexpected top-level field (injection) must be rejected."""
    project = _valid_project()
    project["malicious"] = {"exec": "rm -rf /"}

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_known_good_example_project_passes() -> None:
    """The committed example project must pass validation as-is."""
    yaml = YAML(typ="safe")
    with EXAMPLE_PROJECT.open(encoding="utf-8") as project_file:
        project_data = yaml.load(project_file)

    validate_project_schema(project_data)
