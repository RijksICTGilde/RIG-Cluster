"""Shared guards for project-editing save handlers.

- ``require_project_edit_access``: admin/owner role gate, re-check on every
  mutating request (TOCTOU).
- ``apply_form_data_to_project``: merge submitted form data into the stored
  project; reject submissions that include immutable top-level fields.

Role-based protection (e.g. only admin/owner may edit ``users``) lives in
the role gate; this module does not duplicate that. See
``features/futures/form-field-rbac.md`` for the proper field-level RBAC
that would replace today's per-handler glue.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from opi.core.auth_decorators import get_current_user
from opi.services.project_authorization import (
    get_user_role_for_project,
    is_user_authorized_for_project,
)
from opi.services.project_store import get_project_store


def require_project_edit_access(request: Request, project_name: str):
    """Require admin/owner role on the project. Returns (project, user_email).

    Raises HTTPException 404 if the project does not exist, 403 if the user
    is not authorized for the project or lacks the required role.
    """
    user = get_current_user(request)
    project = get_project_store().get(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Onvoldoende rechten om dit project te bewerken")

    return project, user_email


# Top-level fields no form should ever submit. name=on-disk filename;
# clusters=not yet editable (see features/futures/cluster-editing.md).
IMMUTABLE_PROJECT_FIELDS: tuple[str, ...] = ("name", "clusters")


def apply_form_data_to_project(
    existing_data: dict[str, Any],
    submitted_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge submitted form data into the stored project data. Returns a new dict.

    Raises HTTPException 400 if the submission tries to set any
    ``IMMUTABLE_PROJECT_FIELDS``. That is a structural-integrity violation:
    no form should expose these fields, so receiving one means either a
    form bug or a tampered submission. Fail loud rather than silently
    dropping the value.
    """
    offending = [field for field in IMMUTABLE_PROJECT_FIELDS if field in submitted_data]
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"Submitted form data contains immutable project fields: {', '.join(offending)}",
        )
    return {**existing_data, **submitted_data}
