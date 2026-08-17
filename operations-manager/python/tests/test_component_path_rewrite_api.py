"""The API's `rewrite` next to `path`, measured on what it produces.

The chain the API feeds is: build_component_config writes the path entry into the
project file, ProjectFileHandler reads it back, and ingress.yaml.jinja turns it into
an nginx snippet. The tests below follow that chain rather than the code, because the
question zad-cli asked is what arrives at the container, not what the request model
accepts.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from opi.api.router import AddComponentRequest, UpdateComponentRequest
from opi.api.validation import (
    ADD_COMPONENT_VALIDATORS,
    UPDATE_COMPONENT_VALIDATORS,
    validate_api_payload,
)
from opi.forms.editables.fields.components import COMPONENT_PATH_REWRITE_EDITABLE
from opi.generation.manifests import render_template
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.utils.project_utils import build_component_config


async def _component_and_ingress(rewrite: str | None) -> tuple[dict[str, Any], str]:
    """Build a component through the add-component path and render its ingress."""
    component = await build_component_config(
        name="api",
        component_type="single",
        port=8080,
        path="/api",
        rewrite=rewrite,
        services=["publish-on-web"],
    )
    project_data = {"name": "proj", "components": [component]}
    paths = ProjectFileHandler().extract_component_paths(project_data, "api")
    manifest = render_template(
        "ingress.yaml.jinja",
        {
            "name": "api-ingress",
            "hostname": "api.example.com",
            "path": paths[0]["match"],
            "rewrite": paths[0]["rewrite"],
            "enable_tls": False,
        },
    )
    return component, manifest


# ---------------------------------------------------------------------------
# Add: absent rewrite stays absent, a given rewrite reaches the manifest
# ---------------------------------------------------------------------------


async def test_without_rewrite_the_key_stays_out_and_the_ingress_has_no_rule() -> None:
    """No rewrite asked for means no rewrite written and no rewrite rendered."""
    component, manifest = await _component_and_ingress(None)

    assert component["path"] == [{"match": "/api"}]
    assert "rewrite" not in component["path"][0]
    assert 'rewrite "^/api' not in manifest
    assert "haproxy.router.openshift.io/rewrite-target" not in manifest


async def test_rewrite_lands_in_the_path_entry_and_in_the_ingress() -> None:
    """`rewrite: /` strips the prefix before the request reaches the container."""
    component, manifest = await _component_and_ingress("/")

    assert component["path"] == [{"match": "/api", "rewrite": "/"}]
    assert 'rewrite "^/api/?(.*)$" "/$1" break;' in manifest


async def test_empty_rewrite_is_treated_as_absent() -> None:
    """An empty value is not a rewrite to the root; it leaves the key out entirely."""
    component, manifest = await _component_and_ingress("")

    assert component["path"] == [{"match": "/api"}]
    assert 'rewrite "^/api' not in manifest


# ---------------------------------------------------------------------------
# Validation: one rule for the form and both API sides
# ---------------------------------------------------------------------------


async def test_add_accepts_a_valid_rewrite() -> None:
    payload = {"name": "api", "image": "img:v1", "path": "/api", "rewrite": "/"}
    assert await validate_api_payload(payload, ADD_COMPONENT_VALIDATORS) == payload


@pytest.mark.parametrize("bad", ["geen-slash", "/met spatie"])
async def test_add_rejects_a_bad_rewrite_with_the_form_message(bad: str) -> None:
    """Same validator as the form, so the same message comes out of the API."""
    expected = COMPONENT_PATH_REWRITE_EDITABLE.validator.validate(bad)
    assert expected

    with pytest.raises(HTTPException) as exc:
        await validate_api_payload(
            {"name": "api", "image": "img:v1", "path": "/api", "rewrite": bad},
            ADD_COMPONENT_VALIDATORS,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["field_errors"]["rewrite"] == expected


async def test_update_rejects_a_bad_rewrite() -> None:
    with pytest.raises(HTTPException) as exc:
        await validate_api_payload({"rewrite": "geen-slash"}, UPDATE_COMPONENT_VALIDATORS)

    assert exc.value.status_code == 422
    assert "rewrite" in exc.value.detail["field_errors"]


async def test_update_without_the_field_passes() -> None:
    """A PATCH only carries what it changes; leaving both out is not an error."""
    payload = {"image": "img:v2"}
    assert await validate_api_payload(payload, UPDATE_COMPONENT_VALIDATORS) == payload


def test_both_sides_share_the_rewrite_editable() -> None:
    """One rule, not a second pattern next to the form's."""
    assert ADD_COMPONENT_VALIDATORS["rewrite"] is COMPONENT_PATH_REWRITE_EDITABLE
    assert UPDATE_COMPONENT_VALIDATORS["rewrite"] is COMPONENT_PATH_REWRITE_EDITABLE


# ---------------------------------------------------------------------------
# The request models carry the field, without a default
# ---------------------------------------------------------------------------


def test_request_models_default_to_no_rewrite() -> None:
    assert AddComponentRequest(name="api", image="img:v1").rewrite is None
    assert UpdateComponentRequest().rewrite is None


def test_add_request_accepts_a_rewrite() -> None:
    assert AddComponentRequest(name="api", image="img:v1", path="/api", rewrite="/").rewrite == "/"
