"""De operationele poort van RC-323, sinds RC-392 via ``decide()``.

Jobs, de databaseconsole en terugzetten voeren code uit met databasecredentials of
overschrijven data, dus de leesrol ``member`` mag dat niet terwijl ``developer`` dat wel
mag. Deze tests pinnen dat onderscheid op de nieuwe poort: ``authorize`` weigert
``member`` en laat ``developer``, ``owner`` en de platformbeheerder door, en ``may``
geeft hetzelfde antwoord zonder te raisen (voor de knop in de modal).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.services.project_service import ProjectService, ProjectUser, get_project_service
from opi.services.user_service import get_user_service
from opi.web.authz import authorize, may

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
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session={}, path_params={})


def _operate(email: str, project_name: str) -> tuple[Any, str]:
    return authorize(_request_for(email), action=Action.JOB_RUN, project_name=project_name)


def _may_operate(email: str, project_name: str) -> bool:
    return may(_request_for(email), action=Action.JOB_RUN, project_name=project_name)


def test_operate_roles_excludes_the_read_role() -> None:
    """member is de rol die deze poort uitsluit (RC-323), developer niet."""
    from opi.core.authz.engine import Role
    from opi.services.project_roles import has_role

    assert not has_role("member", Role.DEVELOPER)
    assert all(has_role(rol, Role.DEVELOPER) for rol in ("admin", "owner", "developer"))


def test_member_is_rejected(project_service: ProjectService) -> None:
    with pytest.raises(HTTPException) as exc:
        _operate(MEMBER_EMAIL, PROJECT_NAME)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("email", [DEVELOPER_EMAIL, OWNER_EMAIL, ADMIN_EMAIL])
def test_operate_roles_are_let_through(project_service: ProjectService, email: str) -> None:
    project, user_email = _operate(email, PROJECT_NAME)
    assert project is not None
    assert user_email == email.lower()


def test_unknown_project_is_404(project_service: ProjectService) -> None:
    with pytest.raises(HTTPException) as exc:
        _operate(OWNER_EMAIL, "no-such-project")
    assert exc.value.status_code == 404


def test_may_operate_mirrors_the_gate_without_raising(project_service: ProjectService) -> None:
    assert _may_operate(MEMBER_EMAIL, PROJECT_NAME) is False
    assert _may_operate(DEVELOPER_EMAIL, PROJECT_NAME) is True
    assert _may_operate(OWNER_EMAIL, PROJECT_NAME) is True


def test_may_operate_denies_an_outsider(project_service: ProjectService) -> None:
    """Not a project user at all: no role, so no operate access either."""
    assert _may_operate("stranger@example.com", PROJECT_NAME) is False
