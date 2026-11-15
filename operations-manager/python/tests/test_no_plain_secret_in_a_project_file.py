"""No path can put a ``plain:`` value in a project file any more (schema version 2.18).

``plain:`` marks a secret as deliberately unencrypted, and a project file lives in the
``zad-projects`` git repository, so such a value is readable to everyone who can read that
repository. Five fields in the project schema allowed it, two service config models did, and
the create wizard put the deployments-repo password in unchanged -- which is how five
production project files came to carry that password as ``plain:``.

The checks below are derived from the schema and from the service models, so a secret field
added later is covered without anyone remembering this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from opi.services.project_schema import SCHEMA_PATH, validate_project_schema
from opi.services.registry import SERVICES
from opi.utils.age import AGE_BLOCK_BEGIN, PLAIN_PREFIX

#: What the schema's own ``age-encrypted`` pattern contains; a subschema carrying it is a
#: field that must hold a secret.
AGE_MARKER = AGE_BLOCK_BEGIN

PLAIN_VALUE = f"{PLAIN_PREFIX}geheim"
ENCRYPTED_VALUE = "base64+age:Y2lwaGVydGV4dA=="


def _secret_subschemas(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    """Every subschema that demands an encrypted value, with where it sits.

    Found by its ``pattern``, the same way ``age_pattern_violations`` finds a violation, so
    the two cannot disagree about which fields are secret.
    """
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        if AGE_MARKER in str(node.get("pattern", "")):
            found.append(("/".join(path) or "(root)", node))
        for key, value in node.items():
            found.extend(_secret_subschemas(value, (*path, str(key))))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_secret_subschemas(value, (*path, str(index))))
    return found


def _project_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _service_models() -> list[tuple[str, dict[str, Any]]]:
    """Every config model a service declares, as a JSON schema, per layer."""
    models: list[tuple[str, dict[str, Any]]] = []
    for service in SERVICES.values():
        for layer in service.config_layers():
            model = service.config_model_for(layer)
            if model is not None:
                models.append((f"{service.service_type.value}/{layer.value}", model.model_json_schema()))
    return models


def test_the_project_schema_has_secret_fields_to_judge() -> None:
    # Without this the two tests below would pass on an empty list.
    assert _secret_subschemas(_project_schema()), "no secret field found in the project schema"


def test_no_secret_field_in_the_project_schema_accepts_plain() -> None:
    accepting = [
        where
        for where, subschema in _secret_subschemas(_project_schema())
        if Draft202012Validator(subschema).is_valid(PLAIN_VALUE)
    ]
    assert accepting == []


def test_no_secret_field_in_a_service_model_accepts_plain() -> None:
    accepting = [
        f"{service}:{where}"
        for service, schema in _service_models()
        for where, subschema in _secret_subschemas(schema)
        if Draft202012Validator(subschema).is_valid(PLAIN_VALUE)
    ]
    assert accepting == []


def test_the_whole_project_schema_mentions_plain_nowhere() -> None:
    # A new field cannot reintroduce the form by copying a neighbour.
    assert "^plain:" not in SCHEMA_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "where",
    [
        "repositories/0/password",
        "remote-sources/0/chisel/password",
        "remote-sources/0/services/postgresql-database/password",
        "remote-sources/0/services/minio-storage/access-key",
        "remote-sources/0/services/minio-storage/secret-key",
    ],
)
def test_each_named_field_is_refused_as_plain_and_accepted_encrypted(where: str) -> None:
    """The five fields the ship narrowed, judged through a whole project file."""
    from opi.services.project_schema import ProjectSchemaError

    def _project(value: str) -> dict[str, Any]:
        """Every secret encrypted, and only the field under test set to *value*."""
        project = _project_with_remote_source(ENCRYPTED_VALUE)
        holder: Any = project
        parts = where.split("/")
        for part in parts[:-1]:
            holder = holder[int(part)] if part.isdigit() else holder[part]
        holder[parts[-1]] = value
        return project

    validate_project_schema(_project(ENCRYPTED_VALUE))
    with pytest.raises(ProjectSchemaError, match=where):
        validate_project_schema(_project(PLAIN_VALUE))


def _project_with_remote_source(secret: str) -> dict[str, Any]:
    """A schema-valid project carrying every field the parametrization reaches."""
    return {
        "schema-version": "2.18",
        "name": "plain-guard",
        "description": "Project for the plain-text guard",
        "clusters": ["local"],
        "users": [{"email": "admin@rijksoverheid.nl", "roles": ["admin", "ops"]}],
        "repositories": [
            {
                "name": "main-repo",
                "url": "https://git.example.com/org/repo.git",
                "username": "git",
                "password": secret,
                "branch": "main",
                "path": ".",
            }
        ],
        "remote-sources": [
            {
                "name": "bron",
                "chisel": {
                    "server-url": "https://chisel.example.com",
                    "username": "admin",
                    "password": secret,
                },
                "services": {
                    "postgresql-database": {
                        "host": "db.example.com",
                        "port": 5432,
                        "username": "app",
                        "password": secret,
                        "database": "app",
                        "schema": "public",
                    },
                    "minio-storage": {
                        "host": "s3.example.com",
                        "access-key": secret,
                        "secret-key": secret,
                        "bucket": "app",
                    },
                },
            }
        ],
        "components": [
            {
                "name": "web",
                "type": "deployment",
                "ports": {"inbound": [8080], "outbound": [443]},
                "services": [],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "plain-guard",
                "repository": "main-repo",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            }
        ],
    }


def test_the_guard_reports_a_plain_value_as_plain_text() -> None:
    """``ProjectStore._validate`` runs this one even with ``enforce_validation=False``."""
    from opi.services.project_schema import find_plaintext_secret_violations

    project = _project_with_remote_source(PLAIN_VALUE)

    assert "repositories/0/password" in find_plaintext_secret_violations(project)


def test_the_guard_reports_a_plain_registry_token() -> None:
    from opi.services.project_validation import find_plaintext_service_config_violations

    project = {
        "name": "plain-guard",
        "services": [
            {
                "name": "image-registries",
                "config": {
                    "registries": [{"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": PLAIN_VALUE}]
                },
            }
        ],
    }

    assert find_plaintext_service_config_violations(project) == [
        "services/image-registries/config/registries/0/password"
    ]


def test_the_project_template_carries_no_plain_password() -> None:
    """The wizard's base layer: the setting may be ``plain:``, the project file may not."""
    from opi.core.config import settings
    from opi.forms.editables.template import build_new_project_base_data
    from opi.utils.age import decrypt_password_smart_sync, generate_age_key_pair

    private_key, public_key = generate_age_key_pair()
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(settings, "SOPS_AGE_PRIVATE_KEY", private_key)
        patched.setattr(settings, "SOPS_AGE_PUBLIC_KEY", public_key)
        patched.setattr(settings, "PROJECT_KEK_PUBLIC_KEY", "")
        patched.setattr(settings, "PROJECT_REPO_PASSWORD", "plain:het-echte-wachtwoord")
        base_data = build_new_project_base_data()

    stored = base_data["repositories"][0]["password"]
    assert not stored.startswith(PLAIN_PREFIX)
    assert decrypt_password_smart_sync(stored, private_key) == "het-echte-wachtwoord"


def test_no_committed_schema_fragment_accepts_plain() -> None:
    """The committed per-service fragments, as a file guard next to the model guard."""
    catalog = Path(__import__("opi").__file__).resolve().parent / "services" / "catalog"
    offenders = [
        str(fragment.relative_to(catalog))
        for fragment in sorted(catalog.rglob("*.v*.json"))
        if "^plain:" in fragment.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_a_file_that_still_declares_2_17_is_accepted_at_the_git_gate() -> None:
    """The gate validates against the DECLARED version, and migration runs after it.

    Without this the five production files that carry the deployments-repo password as
    ``plain:`` would be refused at the gate instead of migrated.
    """
    from opi.services.project_schema import validate_declared_project_schema

    project = _project_with_remote_source(PLAIN_VALUE)
    project["schema-version"] = "2.17"
    project.pop("remote-sources")

    validate_declared_project_schema(project)
