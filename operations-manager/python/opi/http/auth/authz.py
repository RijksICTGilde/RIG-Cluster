"""The portal's enforcement point: one dependency that calls ``decide()``.

The action comes from the route itself (``@action``), the resource from one read of the
project file.

What does not work on a project (the portal pages, the platform administration) does not
pass here: those rules carry another :class:`~opi.core.authz.engine.Audience`, and their
enforcement stays the session middleware and ``require_platform_admin`` respectively.

On EVERY decision, a refusal included, the four attributes from design 8.3 land on
``request.state``, so they are there even when the request ends in a 403.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from jinja2 import pass_context
from starlette.requests import Request  # noqa: TC002 -- FastAPI reads the dependency at runtime

from opi.core.actor import user_actor
from opi.core.audit_trail import capture_fields
from opi.core.authz.actions import Action, action_of, query_targets_of
from opi.core.authz.engine import (
    Audience,
    Cause,
    Principal,
    PrincipalKind,
    decide,
    record_decision,
    rule_for,
)
from opi.http.auth.auth_decorators import get_current_user
from opi.manager.clone_validation import new_or_changed_clone_sources
from opi.services.project_members import member_named
from opi.services.project_roles import PLATFORM_ADMIN_ROLES, build_resource, member_roles, resource_path
from opi.services.project_store import get_project_store
from opi.services.step_up import has_fresh_step_up, is_elevated_admin
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.core.authz.engine import Decision, Resource
    from opi.services.project_store import ProjectSummary

logger = logging.getLogger(__name__)

#: The 403 of a role that is too narrow. One text, because :func:`authorize` and
#: :func:`require_action` refuse for the same reason.
ROLE_TOO_NARROW = "Onvoldoende rechten voor deze handeling op dit project"


def portal_principal(request: Request, project: ProjectSummary) -> Principal:
    """The caller as a principal on THIS project.

    A platform admin with elevated rights gets the same roles on every project as they now
    get ``admin`` everywhere (B6); they need not be a member.
    """
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    if is_elevated_admin(get_user_service(), email):
        roles = PLATFORM_ADMIN_ROLES
    else:
        member = member_named(project, email)
        roles = member_roles(member) if member is not None else frozenset()
    return Principal(
        kind=PrincipalKind.HUMAN,
        name=email,
        project=project.name,
        roles=roles,
        actor=user_actor(email),
    )


def authorize(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
    project: ProjectSummary | None = None,
) -> tuple[ProjectSummary, str]:
    """Allow this action on this project, or refuse it. Gives (project, email address).

    A missing step-up is NOT a refusal here: ``@requires_step_up`` has its own answer for
    that (the dialog), and that answer is lost if this gate turns it into a bare 403.

    ``project`` weighs against a project state the store does not hold, see :func:`_weigh`.

    Raises:
        HTTPException: 404 when the project does not exist, 403 when the roles open too
            little.
    """
    if project is None:
        project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    principal = portal_principal(request, project)
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )
    fresh = has_fresh_step_up(request)
    decision = decide(principal, action, resource, step_up_fresh=fresh)
    record_decision(request, action=action, resource=resource, decision=decision, step_up_fresh=fresh)

    if decision.allowed or decision.cause is Cause.STEP_UP_REQUIRED or decision.step_up_would_help:
        return project, principal.name

    logger.info(
        "Geweigerd: %s op '%s' door '%s' (%s)",
        action.value,
        resource.path,
        principal.name,
        decision.cause.value if decision.cause else "onbekend",
    )
    if not principal.roles:
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")
    raise HTTPException(status_code=403, detail=ROLE_TOO_NARROW)


def authorized_project(request: Request) -> ProjectSummary:
    """The project the gate already allowed for THIS request.

    A handler then reads the project file out of the decision that is already there instead
    of calling ``authorize`` again: that would be a second decision about the same question,
    and the audit records a second row for it.

    Only :func:`enforce_route_action` sets this, so that a second ``authorize`` for ANOTHER
    action in the body cannot overwrite the route's project.

    Raises:
        HTTPException: 500 when the gate has not run, because the route is then hung wrong
            and going on without a decision is no option.
    """
    project = getattr(request.state, "authz_project", None)
    if project is None:
        raise HTTPException(status_code=500, detail="Autorisatiepoort niet gedraaid voor deze route")
    return project


def _weigh(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
    project: ProjectSummary | None = None,
) -> tuple[Resource, Decision] | None:
    """The resource and the outcome, or ``None`` when the project does not exist.

    Without :func:`record_decision`: the caller decides whether this weighing is worth
    an audit row. A fresh step-up counts as present here, because it has its own answer
    and is not a refusal on the role.

    ``project`` weighs against a project state the store does not hold: the restore builds
    the deployment it is about to write, and that deployment's own marking is what decides
    (RC-417). Omitted, the stored project is read.
    """
    if project is None:
        project = get_project_store().get(project_name)
    if project is None:
        return None
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )
    return resource, decide(portal_principal(request, project), action, resource, step_up_fresh=True)


def may(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
) -> bool:
    """Whether the ROLES open this action, apart from a fresh step-up.

    For a button. The gate is :func:`authorize`; a button that shows itself while the
    step-up is still to come is the intention, because the dialog follows on the click.
    """
    weighing = _weigh(request, action=action, project_name=project_name, deployment=deployment, component=component)
    return weighing is not None and weighing[1].allowed


def _deployment_names(project: ProjectSummary | None) -> list[str]:
    deployments = (getattr(project, "data", None) or {}).get("deployments") or []
    return [str(d["name"]) for d in deployments if isinstance(d, dict) and d.get("name")]


def may_on_any_deployment(request: Request, *, action: Action, project_name: str) -> bool:
    """Whether this action is open on AT LEAST ONE deployment of this project.

    The floor for a flow that only picks its deployment in a later step. Without a
    deployment an action weighs against the project's highest marking, and then a developer
    in a project with production could not even open the restore wizard while they may
    restore between two non-production deployments (RC-417).
    """
    names = _deployment_names(get_project_store().get(project_name))
    if not names:
        # Without deployments there is no target to weigh on, and then the weighing on the
        # project applies, as with every other action without a deployment.
        return may(request, action=action, project_name=project_name)
    return any(may(request, action=action, project_name=project_name, deployment=name) for name in names)


def require_on_any_deployment(request: Request, needed: Action, project_name: str) -> None:
    """403 when *needed* is open on no deployment of this project at all.

    The real decision is weighed and recorded on the source and the target the flow
    eventually points at. Only the REFUSAL belongs in the audit here, and
    :func:`require_action` records that on the project as a whole.
    """
    if may_on_any_deployment(request, action=needed, project_name=project_name):
        return
    require_action(request, needed, project_name)
    raise HTTPException(status_code=403, detail=ROLE_TOO_NARROW)


def require_action(
    request: Request,
    needed: Action,
    project_name: str,
    *,
    deployment: str | None = None,
    project: ProjectSummary | None = None,
) -> None:
    """403 when the roles do not open *needed*; only the REFUSAL is recorded.

    For a call that does nothing itself: a GET that re-renders a form, or a step of a modal
    wizard. The audit should not get a row about a backup or a restore that did not happen,
    but a refused attempt does belong in it (``features/audit.md``).
    """
    weighing = _weigh(request, action=needed, project_name=project_name, deployment=deployment, project=project)
    if weighing is not None:
        resource, decision = weighing
        if decision.allowed:
            return
        # The weighing assumed a fresh step-up, but the row carries the session as it is: a
        # refusal here is always one on the ROLE, and a row reporting a second factor that
        # was not there misleads an investigation.
        record_decision(
            request, action=needed, resource=resource, decision=decision, step_up_fresh=has_fresh_step_up(request)
        )
    raise HTTPException(status_code=403, detail=ROLE_TOO_NARROW)


def require_clone_source_access(
    request: Request,
    project_name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    extra_sources: Sequence[str] = (),
) -> None:
    """Weigh ``deployment.clone-from`` on the SOURCE of every clone-from this save sets.

    One gate for both write paths out of the portal: adding a deployment with a clone-from,
    and a restore into a new deployment. Cloning from production is ops, wherever it goes.

    ``extra_sources`` are sources the AFTER state does not name: the restore takes over the
    config of a deployment while only the BACKUP source lands in ``clone-from``, so the
    config source cannot be found back from the project file (RC-417 security review).

    Raises:
        HTTPException: 403 when the roles do not open the source.
    """
    sources = new_or_changed_clone_sources(before, after)
    sources.extend(source for source in extra_sources if source and source not in sources)
    for source in sources:
        authorize(request, action=Action.DEPLOYMENT_CLONE_FROM, project_name=project_name, deployment=source)


def _target(request: Request, from_query: frozenset[str], name: str) -> str | None:
    """The deployment or the component this route works on, from the path or the query.

    The query only counts when the route declares itself with ``@targets_in_query``.
    Without that declaration a caller could paste on a deployment the handler ignores, and
    the gate would weigh another resource than the handler uses.
    """
    value = request.path_params.get(name)
    if not value and name in from_query:
        value = request.query_params.get(name)
    return str(value) if value else None


async def enforce_route_action(request: Request) -> None:
    """Enforce THIS route's action, before the handler runs.

    Registered as a router dependency, so that there is no route that forgets the gate.
    Only project rights pass here: a route without an action, a route without
    ``project_name`` in the path, or a rule with another audience goes through.
    """
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    requested = action_of(endpoint) if endpoint is not None else None
    if requested is None or rule_for(requested).audience is not Audience.PROJECT:
        return

    project_name = request.path_params.get("project_name")
    if not project_name:
        return

    from_query = query_targets_of(endpoint)
    project, _ = authorize(
        request,
        action=requested,
        project_name=str(project_name),
        deployment=_target(request, from_query, "deployment_name"),
        component=_target(request, from_query, "component_name"),
    )
    request.state.authz_project = project

    # For the audit, and only AFTER allowing it, as ``enforce`` does it on the API: the hook
    # runs after the answer and can no longer read the form then, but a caller who may do
    # nothing should not get a body parsed.
    await capture_fields(request, endpoint)


@pass_context
def template_can(
    context: Any,
    action: Action | str,
    project: str | None = None,
    deployment: str | None = None,
    component: str | None = None,
) -> bool:
    """The helper ``can(...)`` for the templates.

    A button shows itself when the ROLES suffice; the step-up follows on the click. No role
    name is left in a template, and the button runs along the same rules as the gate behind
    it.

    As a Jinja global with the context alongside and not as a context_processor: a template
    rendered on its own (a fragment, a test) then has the same helper, instead of a
    ``'can' is undefined`` that only shows up on the page. Without a request in the context
    nothing is allowed, because there is nobody to allow anything to.
    """
    request = context.get("request")
    if request is None:
        return False
    name = project or getattr(request, "path_params", {}).get("project_name")
    if not name:
        return False
    return may(
        request,
        action=Action(action),
        project_name=str(name),
        deployment=deployment,
        component=component,
    )
