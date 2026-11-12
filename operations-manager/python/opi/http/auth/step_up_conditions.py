"""The conditions with which a route signs up for a step-up (RC-332).

A route names one; the rule itself sits in
:func:`opi.services.step_up.requires_production_step_up`. Apart from ``opi.services.step_up``
because this reads the project store, which leans back on ``core`` through the forms layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.http.auth.auth_decorators import get_current_user
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_environment import Omgeving, effective_environment
from opi.services.project_store import get_project_store
from opi.services.step_up import StepUpKind, deployment_by_name, requires_production_step_up

if TYPE_CHECKING:
    from starlette.requests import Request


def _project_data(request: Request, project_name: str) -> dict[str, Any]:
    """The project file, or empty for a user who may not get at it.

    Empty reads as "asks for no step-up", so that the route gives its own 403 or 404.
    Otherwise the dialog would give away to everyone that this project touches production. On
    an ``/api/`` path only ``enforce`` fills this field, with the owner of a personal token,
    and that member has already been laid against this project there (RC-322).
    """
    email = str((get_current_user(request) or {}).get("email", ""))
    if email and not is_user_authorized_for_project(project_name, email):
        return {}
    project = get_project_store().get(project_name)
    return (project.data if project is not None else None) or {}


def always_requires_step_up(request: Request, **_: Any) -> bool:  # noqa: ANN401  # the route arguments of the call being weighed
    """Always true: this action asks for a step-up regardless of the environment.

    A modifying admin route touches the whole cluster (RC-377), and a token or a membership
    gives lasting access to a project, outside production too (RC-387). No environment to
    weigh, but still the condition of ``requires_step_up``, so that these actions carry the
    same step-up as the rest.
    """
    return True


def deployment_environment_change(
    request: Request,
    *,
    project_name: str = "",
    deployment_data: Any = None,  # noqa: ANN401  # the body model of the upsert being weighed
    **_: Any,  # noqa: ANN401  # the other route arguments, unused here
) -> bool:
    """The call SETS or LOWERS the OTAP marking of this deployment.

    A call without a marking leaves the existing one. Which changes count sits in
    :func:`opi.services.step_up.production_marking_change`; a deployment that does not exist
    yet goes in there as new, without a before state.
    """
    requested = getattr(deployment_data, "environment", None)
    if requested is None:
        return False
    project_data = _project_data(request, project_name)
    name = str(getattr(deployment_data, "deploymentName", ""))
    existing = deployment_by_name(project_data, name)
    before = {name: effective_environment(existing)} if existing is not None else {}
    after = {name: Omgeving(requested)}
    return requires_production_step_up(StepUpKind.ENVIRONMENT, project_data, change=(before, after))
