"""The component `description` field (RC-402): one editable shared by form, add and update.

Follows the pattern of test_component_path_rewrite_api.py: the field is defined once as
an Editable and reused everywhere, so these tests measure build_component_config, the API
validation profiles and the request models against that one source, rather than copying
the rule.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from opi.api.router import AddComponentRequest, UpdateComponentRequest
from opi.api.validation import (
    ADD_COMPONENT_VALIDATORS,
    UPDATE_COMPONENT_VALIDATORS,
    validate_api_payload,
)
from opi.forms.editables.fields.components import COMPONENT_DESCRIPTION_EDITABLE
from opi.utils.project_utils import build_component_config
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# build_component_config: the key is only there when there is something to say
# ---------------------------------------------------------------------------


async def test_without_description_the_key_stays_out() -> None:
    cfg = await build_component_config(name="web", component_type="single", port=8080, path="/", services=[])

    assert "description" not in cfg


async def test_empty_description_the_key_stays_out() -> None:
    cfg = await build_component_config(
        name="web", component_type="single", port=8080, path="/", services=[], description=""
    )

    assert "description" not in cfg


async def test_a_description_lands_in_the_component() -> None:
    cfg = await build_component_config(
        name="web",
        component_type="single",
        port=8080,
        path="/",
        services=[],
        description="API voor de zoekfunctie, leest uit de gedeelde database",
    )

    assert cfg["description"] == "API voor de zoekfunctie, leest uit de gedeelde database"


# ---------------------------------------------------------------------------
# Validation: one rule for the form and both API sides
# ---------------------------------------------------------------------------


async def test_add_accepts_a_valid_description() -> None:
    payload = {"name": "api", "image": "img:v1", "description": "Wat dit component doet"}
    assert await validate_api_payload(payload, ADD_COMPONENT_VALIDATORS) == payload


async def test_add_rejects_a_description_over_the_limit() -> None:
    too_long = "x" * 501
    expected = COMPONENT_DESCRIPTION_EDITABLE.validator.validate(too_long)
    assert expected

    with pytest.raises(HTTPException) as exc:
        await validate_api_payload(
            {"name": "api", "image": "img:v1", "description": too_long},
            ADD_COMPONENT_VALIDATORS,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["field_errors"]["description"] == expected


async def test_description_at_exactly_the_limit_is_accepted() -> None:
    exactly_500 = "x" * 500
    assert COMPONENT_DESCRIPTION_EDITABLE.validator.validate(exactly_500) == []


async def test_update_without_the_field_passes() -> None:
    """A PATCH only carries what it changes; leaving description out is not an error."""
    payload = {"image": "img:v2"}
    assert await validate_api_payload(payload, UPDATE_COMPONENT_VALIDATORS) == payload


async def test_update_rejects_a_description_over_the_limit() -> None:
    with pytest.raises(HTTPException) as exc:
        await validate_api_payload({"description": "x" * 501}, UPDATE_COMPONENT_VALIDATORS)

    assert exc.value.status_code == 422
    assert "description" in exc.value.detail["field_errors"]


def test_both_sides_share_the_description_editable() -> None:
    """One rule, not a second pattern next to the form's."""
    assert ADD_COMPONENT_VALIDATORS["description"] is COMPONENT_DESCRIPTION_EDITABLE
    assert UPDATE_COMPONENT_VALIDATORS["description"] is COMPONENT_DESCRIPTION_EDITABLE


def test_the_editable_removes_the_key_when_cleared() -> None:
    assert COMPONENT_DESCRIPTION_EDITABLE.remove_when_none is True


# ---------------------------------------------------------------------------
# The request models carry the field, without a default, and enforce the limit
# ---------------------------------------------------------------------------


def test_request_models_default_to_no_description() -> None:
    assert AddComponentRequest(name="api", image="img:v1").description is None
    assert UpdateComponentRequest().description is None


def test_add_request_accepts_a_description() -> None:
    request = AddComponentRequest(name="api", image="img:v1", description="Wat het doet")
    assert request.description == "Wat het doet"


def test_add_request_rejects_a_description_over_the_limit() -> None:
    with pytest.raises(ValidationError):
        AddComponentRequest(name="api", image="img:v1", description="x" * 501)


def test_update_request_rejects_a_description_over_the_limit() -> None:
    with pytest.raises(ValidationError):
        UpdateComponentRequest(description="x" * 501)
