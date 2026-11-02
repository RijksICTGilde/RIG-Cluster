"""Na elke beslissing staan de vier attributen uit ontwerp 8.3 op het verzoek.

Taak 3 haakt daarop in zonder de routes opnieuw aan te raken, dus ze moeten er ook staan
als de beslissing een WEIGERING is: juist die rij wil de audit hebben.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import handhaaf
from opi.api.principal import AccessDenied
from opi.core.authz.actions import Action, action
from opi.core.authz.engine import Cause, Decision, Resource, record_decision
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van, vervaldatum
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.web.authz import authorize

TOKEN = "zad_beheerder"
BEHEERDER = "beheerder@rijksoverheid.nl"
KIJKER = "kijker@rijksoverheid.nl"


def _project() -> ProjectSummary:
    return ProjectSummary(
        name="demo",
        filename="demo.yaml",
        data={"name": "demo", "deployments": [{"name": "live", "environment": "productie"}]},
        users=[
            ProjectUser(
                email=BEHEERDER,
                role="admin",
                type=LidSoort.PERSOON,
                tokens=[ProjectToken(name="t", hash=hash_van(TOKEN), expires=vervaldatum(30))],
            ),
            ProjectUser(email=KIJKER, role="member", type=LidSoort.PERSOON),
        ],
    )


def _verzoek(email: str) -> Any:
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session={}, path_params={})


def _gebruikersdienst() -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.return_value = False
    dienst.is_email_allowed.return_value = True
    return dienst


def _controleer(request: Any, *, action_verwacht: Action, toegestaan: bool) -> Decision:
    beslissing = request.state.authz_decision
    assert request.state.authz_action is action_verwacht
    assert isinstance(request.state.authz_resource, Resource)
    assert isinstance(beslissing, Decision)
    assert isinstance(request.state.authz_step_up, bool)
    assert beslissing.allowed is toegestaan
    return beslissing


# --- portaal ----------------------------------------------------------------


def test_een_toegestane_portaalaanroep_zet_de_vier_attributen() -> None:
    request = _verzoek(BEHEERDER)
    store = MagicMock()
    store.get.return_value = _project()
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=_gebruikersdienst()),
    ):
        authorize(request, action=Action.PROJECT_READ, project_name="demo")

    beslissing = _controleer(request, action_verwacht=Action.PROJECT_READ, toegestaan=True)
    assert beslissing.grant is not None
    assert request.state.authz_resource.path == "project/demo"


def test_een_geweigerde_portaalaanroep_zet_ze_ook() -> None:
    """De weigering is wat de audit juist wil vastleggen."""
    request = _verzoek(KIJKER)
    store = MagicMock()
    store.get.return_value = _project()
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=_gebruikersdienst()),
        pytest.raises(HTTPException) as fout,
    ):
        authorize(request, action=Action.DEPLOYMENT_WRITE, project_name="demo", deployment="live")

    assert fout.value.status_code == 403
    beslissing = _controleer(request, action_verwacht=Action.DEPLOYMENT_WRITE, toegestaan=False)
    assert beslissing.cause is Cause.INSUFFICIENT_ROLE
    assert request.state.authz_resource.path == "project/demo/deployment/live"
    assert request.state.authz_resource.environment == "productie"


# --- API --------------------------------------------------------------------


@action(Action.PROJECT_READ)
async def _leesroute(project_name: str) -> None: ...


@action(Action.DEPLOYMENT_WRITE)
async def _schrijfroute(project_name: str) -> None: ...


@pytest.mark.asyncio
async def test_een_toegestane_api_aanroep_zet_de_vier_attributen() -> None:
    request = _verzoek(BEHEERDER)
    with patch("opi.api.principal.get_user_service", return_value=_gebruikersdienst()):
        await handhaaf(request, TOKEN, _leesroute, _project())

    _controleer(request, action_verwacht=Action.PROJECT_READ, toegestaan=True)


@pytest.mark.asyncio
async def test_een_geweigerde_api_aanroep_zet_ze_ook() -> None:
    project = _project()
    for lid in project.users or []:
        if lid.email == BEHEERDER:
            lid.roles = ["viewer"]
    request = _verzoek(BEHEERDER)
    with (
        patch("opi.api.principal.get_user_service", return_value=_gebruikersdienst()),
        pytest.raises((HTTPException, AccessDenied)),
    ):
        await handhaaf(request, TOKEN, _schrijfroute, project)

    beslissing = _controleer(request, action_verwacht=Action.DEPLOYMENT_WRITE, toegestaan=False)
    assert beslissing.cause is Cause.INSUFFICIENT_ROLE


# --- de lijst: een handeling levert een rij (RC-406) ------------------------


def _resource(pad: str = "project/demo") -> Resource:
    return Resource(project="demo", path=pad)


def test_the_same_decision_twice_yields_one_element() -> None:
    """The audit records a handling once, however many helpers weigh it."""
    request = _verzoek(BEHEERDER)
    beslissing = Decision(allowed=True)

    for _ in range(2):
        record_decision(
            request, action=Action.RESTORE_RUN, resource=_resource(), decision=beslissing, step_up_fresh=False
        )

    assert len(request.state.authz_decisions) == 1
    assert request.state.authz_decision is beslissing


def test_target_and_source_of_a_clonefrom_yield_two_elements() -> None:
    """Two different resources are two questions, so the audit wants both rows."""
    request = _verzoek(BEHEERDER)

    record_decision(
        request,
        action=Action.DEPLOYMENT_WRITE,
        resource=_resource("project/demo/deployment/doel"),
        decision=Decision(allowed=True),
        step_up_fresh=False,
    )
    record_decision(
        request,
        action=Action.DEPLOYMENT_WRITE,
        resource=_resource("project/demo/deployment/bron"),
        decision=Decision(allowed=True),
        step_up_fresh=False,
    )

    assert len(request.state.authz_decisions) == 2


def test_a_refusal_after_a_permission_is_its_own_element() -> None:
    """Same action and resource, different outcome: that is not the same decision."""
    request = _verzoek(BEHEERDER)

    record_decision(
        request, action=Action.RESTORE_RUN, resource=_resource(), decision=Decision(allowed=True), step_up_fresh=False
    )
    record_decision(
        request,
        action=Action.RESTORE_RUN,
        resource=_resource(),
        decision=Decision(allowed=False, cause=Cause.INSUFFICIENT_ROLE),
        step_up_fresh=False,
    )

    assert len(request.state.authz_decisions) == 2
