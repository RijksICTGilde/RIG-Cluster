"""API validation profiles using the editable validation pipeline.

Maps API operations to their relevant Editable validators so that
API endpoints enforce the same business rules as the web form pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from opi.forms.editables.editable import Editable, EditableEnforcer
from opi.forms.editables.fields.components import (
    COMPONENT_IMAGE_EDITABLE,
    COMPONENT_NAME_EDITABLE,
    COMPONENT_PATH_EDITABLE,
    COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
    COMPONENT_USER_ENV_VARS_EDITABLE,
)
from opi.forms.editables.fields.domains import (
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    WIZARD_DEPLOYMENT_NAME_EDITABLE,
)
from opi.forms.editables.pipeline import convert_fields, enforce_rules, validate_fields
from opi.forms.editables.validators import ContainerImageValidator, SlugValidator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation profiles — map API field names to Editable instances
# ---------------------------------------------------------------------------

ADD_COMPONENT_VALIDATORS: dict[str, Editable] = {
    "name": COMPONENT_NAME_EDITABLE,
    "image": COMPONENT_IMAGE_EDITABLE,
    "path": COMPONENT_PATH_EDITABLE,
    "cpu_limit": COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    "memory_limit": COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
    "env_vars": COMPONENT_USER_ENV_VARS_EDITABLE,
}

ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS: dict[str, Editable] = {
    "component_name": Editable(
        yaml_path="components[*]/name",
        validator=COMPONENT_NAME_EDITABLE.validator,
        required=True,
    ),
    "image": COMPONENT_IMAGE_EDITABLE,
}

UPDATE_IMAGE_VALIDATORS: dict[str, Editable] = {
    "newImageUrl": Editable(
        yaml_path="components[*]/image",
        validator=ContainerImageValidator(),
        required=True,
    ),
}

UPSERT_DEPLOYMENT_VALIDATORS: dict[str, Editable] = {
    "deploymentName": Editable(
        yaml_path="deployments[*]/name",
        validator=SlugValidator(),
        required=True,
    ),
}

CREATE_PROJECT_DOMAIN_VALIDATORS: dict[str, Editable] = {
    "domain_format": DOMAIN_FORMAT_EDITABLE,
    "subdomain": DOMAIN_SUBDOMAIN_EDITABLE,
    "base_domain": DOMAIN_BASE_DOMAIN_EDITABLE,
    "deployment_name": WIZARD_DEPLOYMENT_NAME_EDITABLE,
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def validate_api_payload(
    payload: dict[str, Any],
    validators: dict[str, Editable],
    enforcers: list[EditableEnforcer] | None = None,
    context: dict[str, Any] | None = None,
    convert: bool = False,
) -> dict[str, Any]:
    """Validate an API payload using editable validators.

    Runs field-level validation first, then optional enforcers.
    Raises ``HTTPException(422)`` with structured field errors on failure.

    Args:
        payload: The API request data (e.g. from ``model.model_dump()``).
        validators: Mapping of field names to Editable instances.
        enforcers: Optional list of business-rule enforcers.
        context: Optional context dict passed to enforcers.
        convert: If True, apply converter.write() to values and return
                 the converted payload. Otherwise return the original.

    Returns:
        The (optionally converted) payload dict.

    Raises:
        HTTPException: 422 with ``{"detail": {"field_errors": ..., "errors": ...}}``
    """
    field_errors = validate_fields(payload, validators)

    global_errors: list[str] = []
    if enforcers:
        global_errors = await enforce_rules(payload, enforcers, context)

    if field_errors or global_errors:
        detail: dict[str, Any] = {}
        if field_errors:
            detail["field_errors"] = field_errors
        if global_errors:
            detail["errors"] = global_errors
        logger.info("API validation failed: %s", detail)
        raise HTTPException(status_code=422, detail=detail)

    if convert:
        return convert_fields(payload, validators)

    return payload
