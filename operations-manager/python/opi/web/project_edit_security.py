"""Shared guards for project-editing save handlers.

- ``require_project_edit_access``: admin/owner role gate, re-check on every
  mutating request (TOCTOU).
- ``require_project_operate_access``: dezelfde poort voor operationele
  handelingen (job, databaseconsole, terugzetten), waar ``developer`` wel bij
  hoort en de leesrol ``member`` niet.
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
    PROJECT_EDIT_ROLES,
    PROJECT_OPERATE_ROLES,
    get_user_role_for_project,
    is_user_authorized_for_project,
)
from opi.services.project_store import ProjectSummary, get_project_store


def _require_project_role(
    request: Request,
    project_name: str,
    allowed_roles: tuple[str, ...],
    denied_message: str,
) -> tuple[ProjectSummary, str]:
    """Require one of *allowed_roles* on the project. Returns (project, user_email).

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
    if user_role not in allowed_roles:
        raise HTTPException(status_code=403, detail=denied_message)

    return project, user_email


def require_project_edit_access(request: Request, project_name: str):
    """Require admin/owner role on the project. Returns (project, user_email)."""
    return _require_project_role(
        request,
        project_name,
        PROJECT_EDIT_ROLES,
        "Onvoldoende rechten om dit project te bewerken",
    )


def require_project_operate_access(request: Request, project_name: str) -> tuple[ProjectSummary, str]:
    """Require an operational role on the project. Returns (project, user_email)."""
    return _require_project_role(
        request,
        project_name,
        PROJECT_OPERATE_ROLES,
        "Onvoldoende rechten voor deze handeling op dit project",
    )


def may_operate_on_project(request: Request, project_name: str) -> bool:
    """Of deze gebruiker operationele handelingen op dit project mag doen.

    Voor de knop, niet voor de grens: ``require_project_operate_access`` is de grens.
    """
    user = get_current_user(request) or {}
    user_email = user.get("email", "").lower()
    return get_user_role_for_project(project_name, user_email) in PROJECT_OPERATE_ROLES


# Top-level fields no form should ever submit. name=on-disk filename;
# clusters=not yet editable (see features/futures/cluster-editing.md).
IMMUTABLE_PROJECT_FIELDS: tuple[str, ...] = ("name", "clusters")


def apply_form_data_to_project(
    existing_data: dict[str, Any],
    submitted_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge submitted form data into the stored project data. Returns a new dict.

    Raises HTTPException 400 if the submission tries to CHANGE any
    ``IMMUTABLE_PROJECT_FIELDS``. That is a structural-integrity violation:
    no form may set these, so receiving a different value means either a
    form bug or a tampered submission. Fail loud rather than silently
    dropping the value.

    Een veld dat GEMERGDE data meedraagt (de bewerkwizard levert ZIJN hele project -
    de sessie is uit de opgeslagen stand gezaaid) is geen schending: gelijk aan wat er
    al staat is "niet gewijzigd", en de wizard kan daardoor opslaan. Een WIJZIGING blijft
    een weigering.
    """
    offending = [
        field
        for field in IMMUTABLE_PROJECT_FIELDS
        if field in submitted_data and submitted_data[field] != existing_data.get(field)
    ]
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"Submitted form data contains immutable project fields: {', '.join(offending)}",
        )
    return {**existing_data, **submitted_data}
