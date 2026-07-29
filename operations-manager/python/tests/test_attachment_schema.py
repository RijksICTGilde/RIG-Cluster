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
    assert not v.is_valid(
        {"id": "Bad_Id", "filename": "x", "content": "base64+age:abc"}
    )  # bad id pattern (underscore/uppercase)
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
                            "attachments": {"config": [{"reference": "ca", "provide-as": "file", "path": "/etc/ca"}]}
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


def test_duplicate_reference_rejected() -> None:
    # The mft-tp9 bug: the same reference coupled twice, the second with an empty path.
    uses = [
        {"reference": "ca-root", "provide-as": "file", "path": "/etc/fsc/ca/root.pem"},
        {"reference": "ca-root", "provide-as": "file", "path": ""},
    ]
    with pytest.raises(ValueError, match="meervoudig gekoppeld"):
        _assert_unique_attachment_targets(uses, "dirmgr", None)


def test_empty_file_path_rejected() -> None:
    uses = [{"reference": "ca-root", "provide-as": "file", "path": ""}]
    with pytest.raises(ValueError, match="geen pad"):
        _assert_unique_attachment_targets(uses, "dirmgr", None)


def test_missing_env_name_rejected() -> None:
    uses = [{"reference": "tok", "provide-as": "env-var"}]
    with pytest.raises(ValueError, match="geen env-var-naam"):
        _assert_unique_attachment_targets(uses, "api", None)


def test_validate_attachment_couplings_flags_base_component_duplicate() -> None:
    # A base component's services list is not covered by the JSON schema, so this
    # duplicate-with-empty-path can only be caught by validate_attachment_couplings.
    project = {
        "components": [
            {
                "name": "dirmgr",
                "services": [
                    {
                        "attachments": {
                            "config": [
                                {"reference": "ca-root", "provide-as": "file", "path": "/etc/fsc/ca/root.pem"},
                                {"reference": "ca-root", "provide-as": "file", "path": ""},
                            ]
                        }
                    }
                ],
            }
        ],
    }
    from opi.handlers.project_file_handler import validate_attachment_couplings

    errors = validate_attachment_couplings(project)
    assert len(errors) == 1
    assert "meervoudig gekoppeld" in errors[0]


def test_validate_attachment_couplings_passes_valid_project() -> None:
    from opi.handlers.project_file_handler import validate_attachment_couplings

    project = {
        "components": [
            {
                "name": "dirmgr",
                "services": [
                    {
                        "attachments": {
                            "config": [
                                {"reference": "ca-root", "provide-as": "file", "path": "/etc/fsc/ca/root.pem"},
                                {"reference": "dir-cert", "provide-as": "file", "path": "/etc/fsc/cert.pem"},
                            ]
                        }
                    }
                ],
            }
        ],
    }
    assert validate_attachment_couplings(project) == []


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


def test_keycloak_additional_clients_add_resolves_virtualized_path() -> None:
    # Regression: the keycloak "add client" button renders the VIRTUALIZED service
    # path (services -> _services-config), i.e. "_services-config/keycloak/config/
    # additional-clients". _find_sequence_editable must resolve it (via the same
    # apply_virtualize the renderer uses). A brace-only rewrite missed this segment
    # form, so the editable was None, the add produced "" instead of an item, and the
    # button silently no-op'd in production (HTTP 200, no row added).
    from opi.forms.editables.editable import apply_virtualize
    from opi.forms.visualizers.flows import get_flow
    from opi.services.catalog.keycloak.editables import KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE
    from opi.web.router_wizard import _empty_sequence_item, _find_sequence_editable

    flow = get_flow("modal-edit-keycloak-config")
    section = next(s for s in flow.sections if s.section_id == "keycloak-config")

    rendered_path = apply_virtualize(
        KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE.yaml_path,
        KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE.virtualize,
    )
    assert rendered_path == "_services-config/keycloak/config/additional-clients"

    ed = _find_sequence_editable(section, rendered_path)
    assert ed is not None, "virtualized keycloak additional-clients path must resolve (add-client button)"
    # A resolved editable builds a real item, not the empty-string fallback that None produces.
    assert _empty_sequence_item(ed) != ""


def test_service_list_converter_preserves_attachments_data() -> None:
    """Saving the project service selection must NOT drop the attachments catalog
    ``data`` (managed by the Bijlagen UI, absent from the selection form)."""
    from opi.forms.editables.converters import ServiceListConverter

    # The project-level services field opts into catalog-data preservation; the
    # component-level field deliberately does not (it would duplicate the catalog).
    converter = ServiceListConverter(preserve_catalog_data=True)
    existing = {
        "services": [
            "publish-on-web",
            {"keycloak": {"config": {"template": "sso-support"}}},
            {"attachments": {"data": [{"id": "sso", "filename": "c.pem", "content": "AGE..."}]}},
        ]
    }
    # Selection form posts bare service names (attachments still checked, no data).
    out = converter.write(["publish-on-web", "keycloak", "attachments"], context_data=existing)
    attachments = [e for e in out if isinstance(e, dict) and "attachments" in e]
    assert attachments, "attachments entry dropped"
    assert attachments[0]["attachments"]["data"][0]["id"] == "sso"

    # Deselecting attachments removes it (no resurrection from existing data).
    out_removed = converter.write(["publish-on-web"], context_data=existing)
    assert not any(isinstance(e, dict) and "attachments" in e for e in out_removed)
    assert "attachments" not in out_removed

    # No existing catalog -> unchanged.
    assert converter.write(["publish-on-web"], context_data={"services": ["publish-on-web"]}) == ["publish-on-web"]


def test_strip_and_preserve_attachment_content_roundtrip() -> None:
    """The wizard strips attachment content from its session; the save hook re-attaches it
    from the original project data. End-to-end: strip -> capture -> hook restores."""
    import asyncio

    from opi.forms.editables.hooks import PreserveAttachmentContentHook
    from opi.web.router_detail_edit import _strip_attachment_content

    block = "-----BEGIN AGE ENCRYPTED FILE-----\nabc\n-----END AGE ENCRYPTED FILE-----"
    project = {
        "services": [
            "publish-on-web",
            {"attachments": {"data": [{"id": "sso", "filename": "cert.pem", "content": block}]}},
        ]
    }

    # 1. Wizard view: content stripped, metadata kept, original untouched.
    stripped = _strip_attachment_content(project)
    sa = next(e for e in stripped["services"] if isinstance(e, dict) and "attachments" in e)
    assert "content" not in sa["attachments"]["data"][0]
    assert sa["attachments"]["data"][0]["filename"] == "cert.pem"
    oa = next(e for e in project["services"] if isinstance(e, dict) and "attachments" in e)
    assert oa["attachments"]["data"][0]["content"] == block  # deepcopy, original intact

    # 2. Capture original content (as _modal_do_submit does), 3. hook restores at save.
    original_content = {"sso": block}
    saved = _strip_attachment_content(project)  # content-less, like the merged session data
    asyncio.run(PreserveAttachmentContentHook().execute(saved, {"original_attachment_content": original_content}))
    sv = next(e for e in saved["services"] if isinstance(e, dict) and "attachments" in e)
    assert sv["attachments"]["data"][0]["content"] == block

    # No original captured (new project / no attachments) -> hook is a no-op, no crash.
    fresh = _strip_attachment_content(project)
    asyncio.run(PreserveAttachmentContentHook().execute(fresh, {}))
    fv = next(e for e in fresh["services"] if isinstance(e, dict) and "attachments" in e)
    assert "content" not in fv["attachments"]["data"][0]


def test_modal_edit_attachments_flow_carries_services_for_display() -> None:
    """The standalone modal-edit-attachments flow (single ATTACHMENTS_SECTION) must carry
    `services` into step_data via the read-only carrier, so the upload partial can list
    existing attachments even without the services-selection section present."""
    from opi.forms.visualizers.flows import get_flow
    from opi.web.router_wizard import _split_data_across_sections

    flow = get_flow("modal-edit-attachments")
    assert [s.section_id for s in flow.sections] == ["attachments"]

    project = {
        "services": [
            "publish-on-web",
            {"attachments": {"data": [{"id": "sso", "filename": "cert.pem", "content": "AGEBLOCK"}]}},
        ]
    }
    step_data = _split_data_across_sections(flow, project)
    carried = step_data.get("attachments", {}).get("services")
    assert carried, "services not carried into the attachments step"
    catalog = [d for s in carried if isinstance(s, dict) and "attachments" in s for d in s["attachments"]["data"]]
    assert [(d["id"], d["filename"]) for d in catalog] == [("sso", "cert.pem")]


def test_wizard_session_catalog_removal_and_ids() -> None:
    """Removing an existing attachment edits the carried services in step_data (applied on
    save), and _catalog_ids reflects what is already in the catalog for uniqueness checks."""
    from types import SimpleNamespace

    from opi.web.router_wizard_attachments import _catalog_ids, _remove_from_session_catalog

    services = [{"attachments": {"data": [{"id": "sso", "filename": "a"}, {"id": "ca", "filename": "b"}]}}]
    state = SimpleNamespace(
        step_data={"attachments": {"services": services}},
        get_merged_data=lambda: {"services": services},
    )
    assert _catalog_ids(state) == ["sso", "ca"]
    _remove_from_session_catalog(state, "sso")
    remaining = [a["id"] for s in state.step_data["attachments"]["services"] for a in s["attachments"]["data"]]
    assert remaining == ["ca"]
    assert _catalog_ids(state) == ["ca"]


def test_attachment_usage_includes_publish_on_web_provided() -> None:
    """A publish-on-web 'provided' certificate counts as usage (delete-guard + modal), at
    component and deployment-component level; a non-provided tls mode does not."""
    from opi.handlers.project_file_handler import attachment_is_referenced, extract_attachment_usage

    comp = {
        "components": [
            {
                "name": "backend",
                "services": [{"publish-on-web": {"config": {"tls": "provided", "attachment": "real-cert"}}}],
            }
        ]
    }
    assert extract_attachment_usage(comp) == {"real-cert": ["backend"]}
    assert attachment_is_referenced(comp, "real-cert")

    dep = {
        "components": [{"name": "backend"}],
        "deployments": [
            {
                "name": "staging",
                "components": [
                    {
                        "reference": "backend",
                        "services": {"publish-on-web": {"config": {"tls": "provided", "attachment": "real-cert"}}},
                    }
                ],
            }
        ],
    }
    assert extract_attachment_usage(dep) == {"real-cert": ["backend (staging)"]}
    assert attachment_is_referenced(dep, "real-cert")

    standard = {
        "components": [
            {"name": "backend", "services": [{"publish-on-web": {"config": {"tls": "standard", "attachment": "x"}}}]}
        ]
    }
    assert extract_attachment_usage(standard) == {}
    assert not attachment_is_referenced(standard, "x")
