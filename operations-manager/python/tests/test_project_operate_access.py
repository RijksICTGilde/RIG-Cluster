"""Tests for the operational-role gate added in RC-323.

Jobs, de databaseconsole en terugzetten voeren code uit met databasecredentials of
overschrijven data, dus de leesrol ``member`` mag dat niet, in tegenstelling tot
``_require_project_member_access`` (lidmaatschap, elke rol). Deze tests pinnen het
onderscheid: ``require_project_operate_access`` weigert ``member`` en laat
``developer``/``owner``/``admin`` door, en ``may_operate_on_project`` geeft hetzelfde
antwoord zonder te raisen (voor de knop in de modal).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from opi.services.project_authorization import PROJECT_OPERATE_ROLES
from opi.services.project_service import ProjectService, ProjectUser, get_project_service
from opi.services.user_service import get_user_service
from opi.web.project_edit_security import may_operate_on_project, require_project_operate_access

PROJECT_NAME = "operate-target"
MEMBER_EMAIL = "member@example.com"
DEVELOPER_EMAIL = "developer@example.com"
OWNER_EMAIL = "owner@example.com"
ADMIN_EMAIL = "global-admin@example.com"


@pytest.fixture
def project_service() -> ProjectService:
    service = get_project_service()
    saved_projects = dict(service._projects)
    saved_admins = set(get_user_service()._platform_admin_emails)
    service._projects.clear()
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.add(ADMIN_EMAIL)
    service.register(
        project_name=PROJECT_NAME,
        api_key="k",
        filename=f"{PROJECT_NAME}.yaml",
        users=[
            ProjectUser(email=MEMBER_EMAIL, role="member"),
            ProjectUser(email=DEVELOPER_EMAIL, role="developer"),
            ProjectUser(email=OWNER_EMAIL, role="owner"),
        ],
    )
    yield service
    service._projects.clear()
    service._projects.update(saved_projects)
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.update(saved_admins)


def _request_for(email: str) -> Any:
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}))


def test_operate_roles_excludes_the_read_role() -> None:
    """member is the one role this gate exists to exclude (RC-323)."""
    assert "member" not in PROJECT_OPERATE_ROLES
    assert {"admin", "owner", "developer"} <= set(PROJECT_OPERATE_ROLES)


def test_member_is_rejected(project_service: ProjectService) -> None:
    with pytest.raises(HTTPException) as exc:
        require_project_operate_access(_request_for(MEMBER_EMAIL), PROJECT_NAME)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("email", [DEVELOPER_EMAIL, OWNER_EMAIL, ADMIN_EMAIL])
def test_operate_roles_are_let_through(project_service: ProjectService, email: str) -> None:
    project, user_email = require_project_operate_access(_request_for(email), PROJECT_NAME)
    assert project is not None
    assert user_email == email.lower()


def test_unknown_project_is_404(project_service: ProjectService) -> None:
    with pytest.raises(HTTPException) as exc:
        require_project_operate_access(_request_for(OWNER_EMAIL), "no-such-project")
    assert exc.value.status_code == 404


def test_may_operate_mirrors_the_gate_without_raising(project_service: ProjectService) -> None:
    assert may_operate_on_project(_request_for(MEMBER_EMAIL), PROJECT_NAME) is False
    assert may_operate_on_project(_request_for(DEVELOPER_EMAIL), PROJECT_NAME) is True
    assert may_operate_on_project(_request_for(OWNER_EMAIL), PROJECT_NAME) is True


def test_may_operate_denies_an_outsider(project_service: ProjectService) -> None:
    """Not a project user at all: no role, so no operate access either."""
    assert may_operate_on_project(_request_for("stranger@example.com"), PROJECT_NAME) is False
