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


# ---------------------------------------------------------------------------
# Regression tests for review findings (PR #68 augmentation)
# ---------------------------------------------------------------------------


def test_trailing_newline_in_namespace_is_rejected() -> None:
    """Regression: `$` regex anchor allows a final `\\n` under Python `re`.

    Patterns use `\\Z` now; a value ending in `\\n` must be rejected. Without
    this the upstream injection vector (newline-terminated namespace that
    looks valid to the validator but injects YAML structure downstream) was
    silently accepted by the validator.
    """
    project = _valid_project()
    project["deployments"][0]["namespace"] = "valid-project\n"

    with pytest.raises(ProjectSchemaError) as exc:
        validate_project_schema(project)
    assert "namespace" in str(exc.value)


def test_trailing_newline_in_project_name_is_rejected() -> None:
    """Regression: project-name pattern must reject a trailing `\\n`."""
    project = _valid_project()
    project["name"] = "valid-project\n"

    with pytest.raises(ProjectSchemaError) as exc:
        validate_project_schema(project)
    assert "name" in str(exc.value)


def test_env_var_value_with_newline_is_rejected() -> None:
    """Regression: env-var values must reject control chars.

    A newline in an env-var value rendered into a YAML manifest (`value:
    "{{ env_value }}"` in deployment.yaml.jinja) is the canonical injection
    vector that PR #63 closed at the template layer. Schema layer must close
    it at the source.
    """
    project = _valid_project()
    project["deployments"][0]["components"][0]["env-vars"] = {
        "FOO": "value\nLD_PRELOAD=/tmp/x.so",
    }

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_env_var_value_with_carriage_return_is_rejected() -> None:
    """Same as the newline case for \\r."""
    project = _valid_project()
    project["deployments"][0]["components"][0]["env-vars"] = {"FOO": "value\rmore"}

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_env_var_value_with_null_byte_is_rejected() -> None:
    """Embedded NUL must be rejected (truncation attack on downstream tools)."""
    project = _valid_project()
    project["deployments"][0]["components"][0]["env-vars"] = {"FOO": "value\x00x"}

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_env_var_value_plain_string_is_accepted() -> None:
    """Sanity: a normal env-var value (spaces, dashes, equals) still passes."""
    project = _valid_project()
    project["deployments"][0]["components"][0]["env-vars"] = {
        "DATABASE_URL": "postgresql://app:p@ss-w0rd@db:5432/myapp?ssl=true",
        "FEATURE_FLAGS": "auth,csrf,metrics",
    }
    validate_project_schema(project)


def test_mount_path_with_dotdot_traversal_is_rejected() -> None:
    """Regression: mount-path must reject `..` (path traversal).

    The earlier pattern `^/[\\w./-]+$` allowed `/var/../etc/passwd` because
    `..` is not forbidden in the character class. Container-side this can
    escape the intended storage root if any tool resolves the path.
    """
    project = _valid_project()
    project["components"][0]["storage"][0]["mount-path"] = "/var/../etc/passwd"

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_mount_path_with_double_dot_in_middle_is_rejected() -> None:
    """`..` anywhere in the path is rejected, not just at the start."""
    project = _valid_project()
    project["components"][0]["storage"][0]["mount-path"] = "/data/../secrets"

    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_mount_path_normal_value_is_accepted() -> None:
    """Sanity: normal mount paths (dots in filenames are fine) still pass."""
    project = _valid_project()
    project["components"][0]["storage"][0]["mount-path"] = "/data/v1.0/files"
    validate_project_schema(project)


# ---------------------------------------------------------------------------
# Image and repository-URL injection rejection
# ---------------------------------------------------------------------------


def test_image_with_shell_metacharacters_is_rejected() -> None:
    """Container image must reject shell metacharacters.

    `image: "nginx; rm -rf /"` would be rendered into deployment.yaml.jinja
    as `image: "{{ imageURL }}"`; while PR #77 tightens the template, the
    schema should also reject the obvious injection payload at the source.
    """
    project = _valid_project()
    project["deployments"][0]["components"][0]["image"] = "nginx; rm -rf /"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_image_with_space_is_rejected() -> None:
    """Spaces are not part of an image reference and could split shell args."""
    project = _valid_project()
    project["deployments"][0]["components"][0]["image"] = "nginx latest"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_image_with_newline_is_rejected() -> None:
    """A newline in an image rendered as a bare YAML scalar would inject."""
    project = _valid_project()
    project["deployments"][0]["components"][0]["image"] = "nginx\n  privileged: true"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_image_normal_values_are_accepted() -> None:
    """Sanity: regular image refs (incl. registry+port+tag+digest) still pass."""
    valid_refs = [
        "nginx",
        "nginx:latest",
        "tutum/hello-world",
        "nginxdemos/hello",
        "registry.example.com:5000/myorg/myimg:v1.2.3",
        "ghcr.io/owner/repo:sha-abcdef",
        "registry.example.com:5000/myorg/myimg@sha256:abc123",
    ]
    for ref in valid_refs:
        project = _valid_project()
        project["deployments"][0]["components"][0]["image"] = ref
        validate_project_schema(project)  # must not raise


def test_repository_url_with_shell_metacharacters_is_rejected() -> None:
    """Repo url must reject injection payloads like `ssh://x; rm -rf /`."""
    project = _valid_project()
    project["repositories"][0]["url"] = "ssh://x; rm -rf /"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_repository_url_with_unknown_scheme_is_rejected() -> None:
    """Only https/http/ssh/git are accepted; `file://` and friends are not."""
    project = _valid_project()
    project["repositories"][0]["url"] = "file:///etc/passwd"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_repository_url_with_newline_is_rejected() -> None:
    project = _valid_project()
    project["repositories"][0]["url"] = "https://example.com/r.git\n  injected: yes"
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_repository_url_normal_values_are_accepted() -> None:
    """Sanity: real-world repo URLs still pass."""
    valid_urls = [
        "https://github.com/RijksICTGilde/rig-cluster-application-test.git",
        "ssh://git@host.docker.internal:2222/srv/git/example-project-infra.git",
        "http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad.git",
        "git://anonymous.example.com/r.git",
    ]
    for url in valid_urls:
        project = _valid_project()
        project["repositories"][0]["url"] = url
        validate_project_schema(project)


# ---------------------------------------------------------------------------
# Per-component security override (hidden YAML-only feature)
# ---------------------------------------------------------------------------


def test_component_with_security_block_is_accepted() -> None:
    """The optional ``security`` block on a component must pass schema validation."""
    project = _valid_project()
    project["components"][0]["security"] = {
        "run-as-user": 999,
        "run-as-group": 999,
        "fs-group": 999,
    }
    validate_project_schema(project)


def test_component_with_partial_security_block_is_accepted() -> None:
    """Only some fields set is valid (other defaults handled by template)."""
    project = _valid_project()
    project["components"][0]["security"] = {"run-as-user": 1002}
    validate_project_schema(project)


def test_component_security_block_rejects_unknown_field() -> None:
    """additionalProperties:false on the security block must reject typos."""
    project = _valid_project()
    project["components"][0]["security"] = {"runAsUser": 999}  # camelCase, wrong key
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)


def test_component_security_block_rejects_negative_uid() -> None:
    """minimum:0 on UID/GID must reject negative integers."""
    project = _valid_project()
    project["components"][0]["security"] = {"run-as-user": -1}
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(project)
