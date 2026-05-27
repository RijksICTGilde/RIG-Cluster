"""Shared protections for project-editing flows.

Two security mechanisms applied by every web handler that mutates a project
file:

1. Role gate (``require_project_edit_access``): a user must be admin or
   owner on the project to enter or submit an edit. Re-checked on every
   mutating request, not just the initial GET, because the GET may have
   been done by a different user and the role may have been revoked since
   then.

2. Privileged-key merge (``merge_preserving_protected_keys``): submitted
   form data is merged into the stored project but never allowed to
   overwrite ``users`` (role escalation vector), ``config`` (secret
   manipulation), ``name`` (file path on disk), or ``clusters`` (no
   migration logic yet exists -- see features/futures/cluster-editing.md).

The Editable/FormSection layer has no field-level RBAC; until that lands,
every save handler must apply both protections explicitly. This module is
the single source of truth so the two cannot drift apart between handlers.
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


# Top-level project YAML keys that must never be taken from submitted form
# data. Re-derived from the stored project on every save. Per-key reasoning:
#
# - users:    role/membership edits. Form-driven overwrite would let any
#             editor promote themselves to admin or remove other admins.
# - config:   api-key + AGE public/private keys live here. Form-driven
#             overwrite enables secret manipulation or exfiltration.
# - name:     the on-disk filename is keyed on the project name. Renaming
#             via form would orphan the file.
# - clusters: not currently exposed in edit-mode forms (only the create-
#             mode IDENTITY_SECTION has the picker; IDENTITY_EDIT_SECTION
#             omits it). Defense-in-depth: cluster editing is not yet a
#             supported feature. See features/futures/cluster-editing.md.
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
