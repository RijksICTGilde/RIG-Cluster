"""De condities waarmee een route zich aanmeldt voor een step-up (RC-332).

Een route noemt er een; de regel zelf staat in
:func:`opi.core.step_up.requires_production_step_up`. Apart van ``opi.core.step_up``
omdat dit de projectstore leest, en die leunt via de formulierlaag terug op ``core``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.core.auth_decorators import get_current_user
from opi.core.project_environment import Omgeving, effective_environment
from opi.core.step_up import StepUpKind, deployment_by_name, requires_production_step_up
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from starlette.requests import Request


def _project_data(request: Request, project_name: str) -> dict[str, Any]:
    """Het projectbestand, of leeg voor een gebruiker die er niet bij mag.

    Leeg leest als "vraagt geen step-up", zodat de route zelf zijn eigen 403 of 404 geeft.
    Anders zou de dialoog aan iedereen verklappen dat dit project productie raakt. Een
    API-aanroep heeft geen sessiegebruiker; daar is de projectsleutel al gecontroleerd.
    """
    email = str((get_current_user(request) or {}).get("email", ""))
    if email and not is_user_authorized_for_project(project_name, email):
        return {}
    project = get_project_store().get(project_name)
    return (project.data if project is not None else None) or {}


def project_in_production(request: Request, *, project_name: str = "", **_: Any) -> bool:
    """Een van de deployments van het project staat effectief op productie."""
    return requires_production_step_up(StepUpKind.PROJECT, _project_data(request, project_name))


def deployment_in_production(request: Request, *, project_name: str = "", deployment_name: str = "", **_: Any) -> bool:
    """De effectieve markering van deze deployment is productie."""
    return requires_production_step_up(StepUpKind.DEPLOYMENT, _project_data(request, project_name), deployment_name)


def component_in_production(request: Request, *, project_name: str = "", component_name: str = "", **_: Any) -> bool:
    """Een deployment op productie gebruikt dit component."""
    return requires_production_step_up(StepUpKind.COMPONENT, _project_data(request, project_name), component_name)


def deployment_environment_downgrade(
    request: Request, *, project_name: str = "", deployment_data: Any = None, **_: Any
) -> bool:
    """De aanroep haalt een BESTAANDE deployment van productie af.

    Een nieuwe deployment kan niets verlagen, en een aanroep zonder markering laat de
    bestaande staan.
    """
    gevraagd = getattr(deployment_data, "environment", None)
    if gevraagd is None:
        return False
    project_data = _project_data(request, project_name)
    naam = str(getattr(deployment_data, "deploymentName", ""))
    bestaand = deployment_by_name(project_data, naam)
    if bestaand is None:
        return False
    before = {naam: effective_environment(bestaand)}
    after = {naam: Omgeving(gevraagd)}
    return requires_production_step_up(StepUpKind.ENVIRONMENT, project_data, change=(before, after))
