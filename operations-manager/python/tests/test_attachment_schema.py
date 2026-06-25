"""Schema validation for attachment $defs and pure extraction/validation helpers."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from opi.handlers.project_file_handler import (
    _assert_unique_attachment_targets,
    _merge_attachment_uses,
    attachment_is_referenced,
    extract_attachment_catalog,
    extract_component_attachment_uses,
    extract_deployment_component_attachment_uses,
    validate_attachment_references,
)

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "opi" / "schemas" / "project_v2.json").read_text())


def _validator_for(defname: str) -> jsonschema.Draft202012Validator:
    schema = {"$ref": f"#/$defs/{defname}", "$defs": SCHEMA["$defs"]}
    return jsonschema.Draft202012Validator(schema)


# --- attachment-data-entry ---


def test_data_entry_valid() -> None:
    v = _validator_for("attachment-data-entry")
    assert v.is_valid({"id": "mtlskeystore", "filename": "keystore.p12", "content": "base64+age:abc"})
    assert v.is_valid(
        {
            "id": "ca",
            "filename": "ca.pem",
            "content": "-----BEGIN AGE ENCRYPTED FILE-----\nx\n-----END AGE ENCRYPTED FILE-----",
        }
    )


def test_data_entry_invalid() -> None:
    v = _validator_for("attachment-data-entry")
    assert not v.is_valid({"id": "mtlskeystore", "filename": "keystore.p12"})  # missing content
    assert not v.is_valid({"id": "Bad_Id", "filename": "x", "content": "base64+age:abc"})  # bad id pattern (underscore/uppercase)
    assert not v.is_valid({"id": "a" * 41, "filename": "x", "content": "base64+age:abc"})  # >40
    assert not v.is_valid({"id": "ok", "filename": "x", "content": "not-encrypted"})  # content pattern


# --- attachment-use-entry ---


def test_use_entry_file_mode() -> None:
    v = _validator_for("attachment-use-entry")
    assert v.is_valid({"reference": "mtlskeystore", "provide-as": "file", "path": "/etc/tls/keystore.p12"})
    assert not v.is_valid({"reference": "mtlskeystore", "provide-as": "file"})  # file requires path


def test_use_entry_env_mode() -> None:
    v = _validator_for("attachment-use-entry")
    assert v.is_valid({"reference": "ca", "provide-as": "env-var", "env-name": "CA_BUNDLE"})
    assert not v.is_valid({"reference": "ca", "provide-as": "env-var"})  # env-var requires env-name


def test_use_entry_bad_provide_as() -> None:
    v = _validator_for("attachment-use-entry")
    assert not v.is_valid({"reference": "ca", "provide-as": "mount", "path": "/x"})


# --- pure extraction / validation helpers ---


def _sample_project() -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [
            {"attachments": {"data": [{"id": "mtlskeystore", "filename": "keystore.p12", "content": "base64+age:x"}]}},
        ],
        "components": [
            {
                "name": "api",
                "services": [
                    {
                        "attachments": {
                            "config": [{"reference": "mtlskeystore", "provide-as": "file", "path": "/etc/tls/k.p12"}]
                        }
                    },
                ],
            }
        ],
    }


def test_extract_catalog_and_uses() -> None:
    project = _sample_project()
    catalog = extract_attachment_catalog(project)
    assert set(catalog) == {"mtlskeystore"}
    assert catalog["mtlskeystore"]["filename"] == "keystore.p12"
    uses = extract_component_attachment_uses(project["components"][0])
    assert len(uses) == 1
    assert uses[0]["reference"] == "mtlskeystore"


def test_reference_integrity() -> None:
    project = _sample_project()
    assert validate_attachment_references(project) == []
    assert attachment_is_referenced(project, "mtlskeystore") is True
    assert attachment_is_referenced(project, "unknown") is False
    # Break the reference -> error reported
    project["components"][0]["services"][0]["attachments"]["config"][0]["reference"] = "ghost"
    errors = validate_attachment_references(project)
    assert len(errors) == 1
    assert "ghost" in errors[0]


# --- deployment-level coupling: extract + merge + conflict ---


def test_extract_deployment_component_attachment_uses() -> None:
    # Per-deployment coupling lives under ``services.attachments.config`` on the
    # deployment component (services is a map; attachments sits next to the system
    # revision-map entries, and the ``config`` wrapper matches the base-component).
    project = {
        "deployments": [
            {
                "name": "prod",
                "components": [
                    {
                        "reference": "api",
                        "services": {
                            "attachments": {
                                "config": [{"reference": "ca", "provide-as": "file", "path": "/etc/ca"}]
                            }
                        },
                    }
                ],
            }
        ]
    }
    uses = extract_deployment_component_attachment_uses(project, "prod", "api")
    assert len(uses) == 1
    assert uses[0]["reference"] == "ca"
    # wrong deployment / component -> nothing
    assert extract_deployment_component_attachment_uses(project, "staging", "api") == []
    assert extract_deployment_component_attachment_uses(project, "prod", "web") == []


def test_merge_deployment_overrides_base_by_reference() -> None:
    base = [
        {"reference": "ca", "provide-as": "file", "path": "/etc/ca"},
        {"reference": "kc", "provide-as": "env-var", "env-name": "KC"},
    ]
    override = [
        {"reference": "ca", "provide-as": "file", "path": "/etc/prod-ca"},  # overrides base 'ca'
        {"reference": "extra", "provide-as": "file", "path": "/etc/extra"},  # deployment-only
    ]
    merged = _merge_attachment_uses(base, override)
    by_ref = {u["reference"]: u for u in merged}
    assert len(merged) == 3
    assert by_ref["ca"]["path"] == "/etc/prod-ca"  # deployment wins
    assert by_ref["kc"]["env-name"] == "KC"  # base kept
    assert by_ref["extra"]["path"] == "/etc/extra"  # deployment-only added


def test_duplicate_path_rejected() -> None:
    uses = [
        {"reference": "a", "provide-as": "file", "path": "/etc/x"},
        {"reference": "b", "provide-as": "file", "path": "/etc/x"},
    ]
    with pytest.raises(ValueError, match="hetzelfde pad"):
        _assert_unique_attachment_targets(uses, "api", "prod")


def test_duplicate_env_name_rejected() -> None:
    uses = [
        {"reference": "a", "provide-as": "env-var", "env-name": "TOK"},
        {"reference": "b", "provide-as": "env-var", "env-name": "TOK"},
    ]
    with pytest.raises(ValueError, match="dezelfde env-var"):
        _assert_unique_attachment_targets(uses, "api", None)


def test_distinct_targets_pass() -> None:
    uses = [
        {"reference": "a", "provide-as": "file", "path": "/etc/a"},
        {"reference": "b", "provide-as": "file", "path": "/etc/b"},
        {"reference": "c", "provide-as": "env-var", "env-name": "C"},
    ]
    _assert_unique_attachment_targets(uses, "api", None)  # no raise


def test_attachment_id_allows_hyphens_rejects_underscores() -> None:
    from opi.forms.editables.validators import AttachmentIdValidator

    v = AttachmentIdValidator()
    assert v.validate("my-cert") == []  # hyphen allowed (DNS-1123 safe)
    assert v.validate("ca") == []
    assert v.validate("a") == []
    assert v.validate("my_cert")  # underscore rejected (invalid in a Secret/volume name)
    assert v.validate("my-")  # trailing hyphen rejected
    assert v.validate("-x")  # leading hyphen rejected
    assert v.validate("frontend-tls-keystore") == []  # descriptive name within 40
    assert v.validate("a" * 41)  # over 40 chars rejected


def test_deployment_attachment_sequence_add_builds_proper_item() -> None:
    # The per-deployment coupling lives under ``services.attachments.config`` (deeply
    # nested: deployments[*]/components[*]/services/attachments/config). Sequence-add
    # must resolve the editable and build a proper empty item (provide-as=file), not "".
    from opi.forms.visualizers.wizard_sections import build_deployment_edit_section
    from opi.web.router_wizard import _empty_sequence_item, _find_sequence_editable

    section = build_deployment_edit_section(0, component_count=2)
    path = "deployments[0]/components[0]/services/attachments/config"
    ed = _find_sequence_editable(section, path)
    assert ed is not None
    assert _empty_sequence_item(ed) == {"provide-as": "file"}
