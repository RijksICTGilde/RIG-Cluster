"""API validation profiles using the editable validation pipeline.

Maps API operations to their relevant Editable validators so that
API endpoints enforce the same business rules as the web form pipeline.

Every profile below points at the *shared* editable for the field - the same
object the wizard section renders. Hand-written copies used to sit here
alongside the shared ones; a rule changed on one side then validated
differently on the other, and nothing noticed.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from opi.forms.editables.fields.components import (
    COMPONENT_IMAGE_EDITABLE,
    COMPONENT_NAME_EDITABLE,
    COMPONENT_PATH_MATCH_EDITABLE,
    COMPONENT_PATH_REWRITE_EDITABLE,
    COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
)
from opi.forms.editables.fields.domains import (
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    WIZARD_DEPLOYMENT_NAME_EDITABLE,
)
from opi.forms.editables.pipeline import convert_fields, enforce_rules, validate_fields
from opi.services.catalog.user_env_vars.editables import COMPONENT_USER_ENV_VARS_EDITABLE

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable, EditableEnforcer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation profiles - map API field names to Editable instances
# ---------------------------------------------------------------------------


def _required(editable: Editable) -> Editable:
    """The same field, but the API insists on it.

    Whether a field may be left out is the endpoint's business; what the value
    must look like is the field's. Only the first is overridden here, so the
    rule itself stays in one place.
    """
    return replace(editable, required=True)


def _optional(editable: Editable) -> Editable:
    """The same field, but the API accepts leaving it out."""
    return replace(editable, required=False)


ADD_COMPONENT_VALIDATORS: dict[str, Editable] = {
    "name": COMPONENT_NAME_EDITABLE,
    "image": COMPONENT_IMAGE_EDITABLE,
    "path": _optional(COMPONENT_PATH_MATCH_EDITABLE),
    "rewrite": COMPONENT_PATH_REWRITE_EDITABLE,
    "cpu_limit": COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    "memory_limit": COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
    "env_vars": COMPONENT_USER_ENV_VARS_EDITABLE,
}

#: The partial update of a component. The SAME editables as the add side, so a field is
#: judged by one rule no matter which endpoint writes it; all optional here because a
#: PATCH only carries the fields it changes (an absent value validates clean).
#:
#: The two resource limits belong here for a reason that is easy to lose: the platform
#: ceiling (``max_memory_limit_mi`` / ``max_cpu_limit_m``) used to be enforced twice over
#: -- once by these editables on the add side and on the form, and once by the auto-tuner,
#: which clamped anything too generous back on its next sweep. This profile validated
#: neither limit, so a PATCH with ``{"memory_limit": "64Gi"}`` went straight through and
#: the tuner put it right that night. Since RC-141 a hand-set value is pinned and the
#: tuner leaves it alone, so that second line is gone and this profile is the only place
#: the ceiling still gets checked on this route.
UPDATE_COMPONENT_VALIDATORS: dict[str, Editable] = {
    "path": _optional(COMPONENT_PATH_MATCH_EDITABLE),
    "rewrite": COMPONENT_PATH_REWRITE_EDITABLE,
    "cpu_limit": COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE,
    "memory_limit": COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE,
}

ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS: dict[str, Editable] = {
    "component_name": _required(COMPONENT_NAME_EDITABLE),
    "image": COMPONENT_IMAGE_EDITABLE,
}

UPDATE_IMAGE_VALIDATORS: dict[str, Editable] = {
    "newImageUrl": _required(COMPONENT_IMAGE_EDITABLE),
}

UPSERT_DEPLOYMENT_VALIDATORS: dict[str, Editable] = {
    "deploymentName": _required(WIZARD_DEPLOYMENT_NAME_EDITABLE),
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
        return convert_fields(payload, validators, context_data=context)

    return payload
