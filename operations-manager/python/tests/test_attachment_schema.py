"""Schema validation for attachment $defs and pure extraction/validation helpers."""

import json
from pathlib import Path
from typing import Any

import jsonschema
from opi.handlers.project_file_handler import (
    attachment_is_referenced,
    extract_attachment_catalog,
    extract_component_attachment_uses,
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
    assert not v.is_valid({"id": "Bad-Id", "filename": "x", "content": "base64+age:abc"})  # bad id pattern
    assert not v.is_valid({"id": "toolongidentifier", "filename": "x", "content": "base64+age:abc"})  # >12
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
