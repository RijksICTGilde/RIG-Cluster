"""``require_action`` (opi/web/authz.py): 403 op een te smalle rol, en alleen de
WEIGERING wordt een beslissing.

Voor een aanroep die zelf niets doet: een GET die een formulier herrendert
(``backup_select_deployment``, ``restore_select_mode``) of een stap van een
modal-wizard. De audit hoort geen rij te krijgen over een back-up of een herstel dat
niet gebeurde, maar een geweigerde poging hoort er wel in te staan (RC-406).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.core.leden import LidSoort
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import TE_SMALLE_ROL, require_action

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opi.core.authz.engine import RecordedDecision

PROJECT_NAME = "demo"
OPS = "ops@rijksoverheid.nl"
KIJKER = "kijker@rijksoverheid.nl"


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT_NAME,
        filename=f"{PROJECT_NAME}.yaml",
        data={"name": PROJECT_NAME, "deployments": [{"name": "live", "environment": "acceptatie"}]},
        users=[
            ProjectUser(email=OPS, roles=["admin", "ops"], type=LidSoort.PERSOON),
            ProjectUser(email=KIJKER, roles=["viewer"], type=LidSoort.PERSOON),
        ],
    )


def _request(email: str) -> Any:
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session={}, path_params={})


@contextlib.contextmanager
def _store(project: ProjectSummary | None) -> Iterator[None]:
    store = MagicMock()
    store.get.return_value = project
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    ):
        yield


def _genomen(request: Any) -> list[RecordedDecision]:
    return getattr(request.state, "authz_decisions", [])


def test_a_permitted_role_passes_without_recording_a_decision() -> None:
    request = _request(OPS)

    with _store(_project()):
        require_action(request, Action.BACKUP_CREATE, PROJECT_NAME)

    assert _genomen(request) == []
    assert not hasattr(request.state, "authz_decision")


def test_a_role_too_narrow_gives_403_with_the_fixed_text() -> None:
    with _store(_project()), pytest.raises(HTTPException) as info:
        require_action(_request(KIJKER), Action.RESTORE_RUN, PROJECT_NAME)

    assert info.value.status_code == 403
    assert info.value.detail == TE_SMALLE_ROL


def test_a_refusal_is_recorded_so_the_audit_sees_the_attempt() -> None:
    request = _request(KIJKER)

    with _store(_project()), pytest.raises(HTTPException):
        require_action(request, Action.RESTORE_RUN, PROJECT_NAME)

    genomen = _genomen(request)
    assert len(genomen) == 1
    assert genomen[0].action is Action.RESTORE_RUN
    assert genomen[0].decision.allowed is False
    # The weighing lets a fresh step-up in, the row carries the session as it is.
    assert genomen[0].step_up is False


def test_an_unknown_project_gives_403_without_a_resource_to_record() -> None:
    request = _request(OPS)

    with _store(None), pytest.raises(HTTPException) as info:
        require_action(request, Action.BACKUP_CREATE, PROJECT_NAME)

    assert info.value.status_code == 403
    assert _genomen(request) == []
