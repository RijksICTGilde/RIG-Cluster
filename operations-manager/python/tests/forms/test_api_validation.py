"""Tests for API validation profiles and validate_api_payload helper."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from opi.api.validation import (
    ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS,
    ADD_COMPONENT_VALIDATORS,
    UPDATE_IMAGE_VALIDATORS,
    UPSERT_DEPLOYMENT_VALIDATORS,
    validate_api_payload,
)
from opi.forms.editables.converters import ContainerImageConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import ContainerImageValidator

# ---------------------------------------------------------------------------
# validate_api_payload
# ---------------------------------------------------------------------------


class TestValidateApiPayload:
    def test_valid_payload_passes(self):
        validators = {
            "name": Editable(yaml_path="x", required=True),
        }
        result = validate_api_payload({"name": "myapp"}, validators)
        assert result["name"] == "myapp"

    def test_invalid_payload_raises_422(self):
        validators = {
            "name": Editable(yaml_path="x", required=True),
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({"name": ""}, validators)
        assert exc_info.value.status_code == 422
        assert "field_errors" in exc_info.value.detail
        assert "name" in exc_info.value.detail["field_errors"]

    def test_missing_required_raises_422(self):
        validators = {
            "name": Editable(yaml_path="x", required=True),
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({}, validators)
        assert exc_info.value.status_code == 422

    def test_convert_applies_converters(self):
        validators = {
            "image": Editable(
                yaml_path="x",
                validator=ContainerImageValidator(),
                converter=ContainerImageConverter(),
            ),
        }
        # lowercase image passes validation
        result = validate_api_payload({"image": "nginx:latest"}, validators, convert=True)
        assert result["image"] == "nginx:latest"

    def test_convert_with_uppercase_fails_validation(self):
        validators = {
            "image": Editable(
                yaml_path="x",
                validator=ContainerImageValidator(),
                converter=ContainerImageConverter(),
            ),
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({"image": "Nginx:Latest"}, validators, convert=True)
        assert exc_info.value.status_code == 422

    def test_enforcer_error_in_response(self):
        class FailingEnforcer:
            def enforce(self, value, context):
                raise ValueError("Business rule violated")

        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({}, {}, enforcers=[FailingEnforcer()])
        assert exc_info.value.status_code == 422
        assert "errors" in exc_info.value.detail
        assert "Business rule violated" in exc_info.value.detail["errors"]


# ---------------------------------------------------------------------------
# ADD_COMPONENT_VALIDATORS profile
# ---------------------------------------------------------------------------


class TestAddComponentProfile:
    def test_valid_component(self):
        payload = {
            "name": "myapp",
            "image": "nginx:latest",
            "path": "/",
            "cpu_limit": "500m",
            "memory_limit": "512Mi",
        }
        result = validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert result == payload

    def test_invalid_component_name(self):
        payload = {"name": "MY-APP", "image": "nginx:latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "name" in exc_info.value.detail["field_errors"]

    def test_invalid_image(self):
        payload = {"name": "myapp", "image": "Nginx:Latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "image" in exc_info.value.detail["field_errors"]

    def test_invalid_path(self):
        payload = {"name": "myapp", "image": "nginx:latest", "path": "no-slash"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "path" in exc_info.value.detail["field_errors"]

    def test_invalid_cpu_limit(self):
        payload = {"name": "myapp", "image": "nginx:latest", "cpu_limit": "999m"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "cpu_limit" in exc_info.value.detail["field_errors"]

    def test_invalid_memory_limit(self):
        payload = {"name": "myapp", "image": "nginx:latest", "memory_limit": "2Gi"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "memory_limit" in exc_info.value.detail["field_errors"]

    def test_optional_fields_absent_ok(self):
        payload = {"name": "myapp", "image": "nginx:latest"}
        result = validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert result == payload

    def test_component_name_too_long(self):
        payload = {"name": "abcdefghijklm", "image": "nginx:latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_VALIDATORS)
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS profile
# ---------------------------------------------------------------------------


class TestAddComponentToDeploymentProfile:
    def test_valid(self):
        payload = {"component_name": "myapp", "image": "nginx:latest"}
        result = validate_api_payload(payload, ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS)
        assert result == payload

    def test_invalid_component_name(self):
        payload = {"component_name": "MY-APP", "image": "nginx:latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS)
        assert exc_info.value.status_code == 422

    def test_missing_required_component_name(self):
        payload = {"image": "nginx:latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "component_name" in exc_info.value.detail["field_errors"]


# ---------------------------------------------------------------------------
# UPDATE_IMAGE_VALIDATORS profile
# ---------------------------------------------------------------------------


class TestUpdateImageProfile:
    def test_valid(self):
        payload = {"newImageUrl": "nginx:latest"}
        result = validate_api_payload(payload, UPDATE_IMAGE_VALIDATORS)
        assert result == payload

    def test_uppercase_image(self):
        payload = {"newImageUrl": "Nginx:Latest"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, UPDATE_IMAGE_VALIDATORS)
        assert exc_info.value.status_code == 422
        assert "newImageUrl" in exc_info.value.detail["field_errors"]

    def test_missing_required(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({}, UPDATE_IMAGE_VALIDATORS)
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# UPSERT_DEPLOYMENT_VALIDATORS profile
# ---------------------------------------------------------------------------


class TestUpsertDeploymentProfile:
    def test_valid(self):
        payload = {"deploymentName": "production"}
        result = validate_api_payload(payload, UPSERT_DEPLOYMENT_VALIDATORS)
        assert result == payload

    def test_invalid_slug(self):
        payload = {"deploymentName": "Production!"}
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload(payload, UPSERT_DEPLOYMENT_VALIDATORS)
        assert exc_info.value.status_code == 422

    def test_missing_required(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_api_payload({}, UPSERT_DEPLOYMENT_VALIDATORS)
        assert exc_info.value.status_code == 422
