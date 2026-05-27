"""Shared guards for project-editing save handlers.

- ``require_project_edit_access``: admin/owner role gate, re-check on every
  mutating request (TOCTOU).
- ``merge_preserving_protected_keys`` + ``PROTECTED_PROJECT_KEYS``: drop
  privileged top-level keys from submitted form data.

Until ``Editable`` gets field-level RBAC every save handler must apply both;
see ``features/futures/form-field-rbac.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from opi.core.auth_decorators import get_current_user
from opi.services.project_service import get_project_service


def require_project_edit_access(request: Request, project_name: str):
    """Require admin/owner role on the project. Returns (project, user_email).

    Raises HTTPException 404 if the project does not exist, 403 if the user
    is not authorized for the project or lacks the required role.
    """
    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Onvoldoende rechten om dit project te bewerken")

    return project, user_email


# Always re-derived from the stored project; submit cannot overwrite or
# introduce these. users=role escalation, config=secret exfiltration,
# name=on-disk filename, clusters=not yet editable post-creation (see
# features/futures/cluster-editing.md).
PROTECTED_PROJECT_KEYS: tuple[str, ...] = ("users", "config", "name", "clusters")


def merge_preserving_protected_keys(
    existing_data: dict[str, Any],
    submitted_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge submitted form data into existing project data, preserving the
    privileged top-level keys. Returns a new dict.

    Protected keys are always re-derived from ``existing_data`` regardless
    of what ``submitted_data`` contains. If a protected key is absent from
    ``existing_data``, it stays absent (a submitted value cannot introduce
    it).
    """
    merged = {**existing_data, **submitted_data}
    for key in PROTECTED_PROJECT_KEYS:
        if key in existing_data:
            merged[key] = existing_data[key]
        else:
            merged.pop(key, None)
    return merged
