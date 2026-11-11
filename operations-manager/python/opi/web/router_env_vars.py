"""Viewing and changing the environment variables of a component.

The detail page shows only their names and their number; this window shows and writes the real
values, and it always asks for a fresh step-up, whatever the OTAP marking of the deployment is.

The generic edit wizard and the component modals do NOT touch ``user-env-vars``: no form hook
of the service grabs the field, so saving along those routes leaves the stored value alone.

The window sends the whole set, so it writes through the REPLACE operation of the shared
values use case: the same validation, the same canonical storage shape and the same
re-apply-on-conflict as the v2 values endpoints. Saving therefore also weighs the same
action as those endpoints, ``SERVICE_WRITE_VALUES``, which asks about the PROJECT: a
developer on a project that has production no longer writes values anywhere in it. Opening
the window stays ``ENV_VARS_MANAGE`` on the component, because reading is not writing; who
may open it but not save sees the values without the save button.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.authz.actions import Action, action, targets_in_query
from opi.http.auth.auth_decorators import requires_sso, requires_step_up
from opi.http.auth.authz import authorized_project, may
from opi.http.render.lotc_switch import render_fragment
from opi.manager.project_manager import ProjectManager
from opi.manager.service.errors import ServiceChangeError
from opi.manager.service.progress import NullProgress
from opi.manager.service.values import ComponentValuesChange, set_component_values
from opi.services.catalog.base import ConfigLayer
from opi.services.component_values import ValuesOperation, locate, to_block
from opi.services.project_env_vars import read_user_env_vars
from opi.services.project_utils import affected_deployments
from opi.services.services_enums import ServiceType
from opi.services.step_up import StepUpAction
from opi.utils.age import get_decoded_project_private_key
from opi.utils.env_vars import validate_and_parse_env_vars
from opi.web.task_progress import create_task_and_render_progress

logger = logging.getLogger(__name__)

env_vars_web_router = APIRouter(prefix="/projects", tags=["env-vars-web"])


def _always_step_up(_request: Request, **_route_arguments: str | None) -> bool:
    """Writing values always asks for a fresh second factor, whatever the marking says.

    Declared here rather than read off the grant: ``SERVICE_WRITE_VALUES`` carries no
    step-up requirement, because a service-account token can never answer one, and a grant
    that demands it would lock every API client out of its own values.
    """
    return True


def _layer(deployment_name: str | None) -> ConfigLayer:
    return ConfigLayer.COMPONENT if deployment_name is None else ConfigLayer.DEPLOYMENT_COMPONENT


def _holder_or_404(project_data: dict[str, Any], component_name: str, deployment_name: str | None) -> dict[str, Any]:
    """The dict in the project file that carries the ``user-env-vars``, or a 404."""
    holder = locate(project_data, _layer(deployment_name), component_name, deployment_name)
    if holder is not None:
        return holder
    if deployment_name is None:
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' niet gevonden")
    raise HTTPException(
        status_code=404, detail=f"Component '{component_name}' niet gevonden in deployment '{deployment_name}'"
    )


def _window_url(project_name: str, component_name: str, deployment_name: str | None) -> str:
    if deployment_name is None:
        return f"/projects/{project_name}/componenten/{component_name}/env-vars"
    return f"/projects/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars"


def _scope_label(component_name: str, deployment_name: str | None) -> str:
    """For the log, the commit and the progress line: the scope without the values."""
    if deployment_name is None:
        return f"component '{component_name}'"
    return f"component '{component_name}' in deployment '{deployment_name}'"


async def _window(
    request: Request,
    project_name: str,
    component_name: str,
    deployment_name: str | None,
    *,
    vars_text: str,
    error: str = "",
    can_write: bool = True,
) -> HTMLResponse:
    """The window itself: what is stored (or what was typed) plus why it was not stored.

    ``can_write`` is false for a viewer whose roles open the window but not the save, and
    then the window shows the values without the save button. Letting them type a value the
    POST answers with a 403 is a dead end, and the typing happened in a secret.
    """
    content = render_fragment(
        request,
        template="bg/_env-vars-venster.html.j2",
        context={
            # ``request`` explicitly: ``render_fragment`` does not add it, and the template
            # reads ``request.state.csrf_token`` for the form.
            "request": request,
            "project_name": project_name,
            "component_name": component_name,
            "deployment_name": deployment_name,
            "vars_text": vars_text,
            "fout": error,
            "can_write": can_write,
            "post_url": _window_url(project_name, component_name, deployment_name),
        },
    )
    return HTMLResponse(content=content)


@env_vars_web_router.get(
    "/{project_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@action(Action.ENV_VARS_MANAGE)
@targets_in_query("deployment_name")
@env_vars_web_router.get(
    "/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@requires_sso
@requires_step_up(action=StepUpAction.ENV_VARS_MANAGE)
async def show_env_vars_window(
    request: Request, project_name: str, component_name: str, deployment_name: str | None = None
) -> HTMLResponse:
    """The edit window with the real values; only after a fresh step-up."""
    project = authorized_project(request)
    project_data = project.data or {}
    holder = _holder_or_404(project_data, component_name, deployment_name)

    where = _scope_label(component_name, deployment_name)
    try:
        private_key = await get_decoded_project_private_key(project_data)
        parsed = await read_user_env_vars(holder.get("user-env-vars"), private_key, where=where)
    except (KeyError, ValueError) as exc:
        logger.warning("Projectsleutel van %s niet te lezen voor %s: %s", project_name, where, exc)
        parsed = None

    can_write = may(request, action=Action.SERVICE_WRITE_VALUES, project_name=project_name)
    if parsed is None:
        vars_text = ""
        error = (
            "De opgeslagen variabelen zijn niet te lezen. Opslaan vervangt ze door wat je hier invult."
            if can_write
            else "De opgeslagen variabelen zijn niet te lezen."
        )
    else:
        vars_text = to_block(parsed)
        error = ""
    return await _window(
        request,
        project_name,
        component_name,
        deployment_name,
        vars_text=vars_text,
        error=error,
        can_write=can_write,
    )


@env_vars_web_router.post(
    "/{project_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@action(Action.SERVICE_WRITE_VALUES)
@targets_in_query("deployment_name")
@env_vars_web_router.post(
    "/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@requires_sso
@requires_step_up(_always_step_up, action=StepUpAction.ENV_VARS_MANAGE, fields=("vars",))
async def save_env_vars(
    request: Request, project_name: str, component_name: str, deployment_name: str | None = None
) -> Response:
    """Write the variables of this scope and roll the change out to its deployments.

    The decorator checks the step-up again, so once the window has run out the OTP question
    follows instead of a save.
    """
    text = str((await request.form()).get("vars", ""))
    try:
        values = validate_and_parse_env_vars(text)
    except (ValueError, TypeError) as exc:
        return await _window(request, project_name, component_name, deployment_name, vars_text=text, error=str(exc))

    project_data = authorized_project(request).data or {}
    _holder_or_404(project_data, component_name, deployment_name)
    deployment_names = affected_deployments(
        project_data, deployment_name=deployment_name, component_names=[component_name]
    )

    change = ComponentValuesChange(
        project_name=project_name,
        service_name=ServiceType.USER_ENV_VARS.value,
        layer=_layer(deployment_name),
        operation=ValuesOperation.REPLACE,
        component_name=component_name,
        deployment_name=deployment_name,
        values=values,
        # Not here: the rollout runs as the task whose progress this route answers with.
        rollout=False,
        deployment_names=deployment_names,
    )
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await set_component_values(change, project_manager, NullProgress())
    except ServiceChangeError as refusal:
        logger.warning("Opslag van omgevingsvariabelen geweigerd voor %s: %s", project_name, refusal.error_type)
        return await _window(request, project_name, component_name, deployment_name, vars_text=text, error=str(refusal))
    finally:
        await project_manager.close()

    where = _scope_label(component_name, deployment_name)
    if not result.changed:
        logger.info("Omgevingsvariabelen van %s ongewijzigd, geen opslag", where)
        return _unchanged_response(request, project_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="rollout_project",
        payload={"project_name": project_name, "deployment_names": deployment_names},
        current_step=f"Omgevingsvariabelen van {where} opgeslagen; project wordt verwerkt...",
        success_message=f"Omgevingsvariabelen van {where} opgeslagen",
    )


def _unchanged_response(request: Request, project_name: str) -> Response:
    """Nothing changed: back to the components page, as a full navigation under htmx."""
    url = f"/projects/{project_name}/componenten"
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)
