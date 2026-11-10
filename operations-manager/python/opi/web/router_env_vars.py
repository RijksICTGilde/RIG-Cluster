"""Viewing and changing the environment variables of a component.

The detail page shows only their names and their number; this window shows and writes the real
values, and it always asks for a fresh step-up, whatever the OTAP marking of the deployment is.

The generic edit wizard and the component modals do NOT touch ``user-env-vars``: no form hook
of the service grabs the field, so saving along those routes leaves the stored value alone.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.authz.actions import Action, action, targets_in_query
from opi.forms.editables.converters import KeyValueConverter, keep_existing_ciphertext_if_unchanged
from opi.forms.editables.validators import KeyValueValidator
from opi.http.auth.auth_decorators import requires_sso, requires_step_up
from opi.http.auth.authz import authorized_project
from opi.http.render.lotc_switch import render_fragment
from opi.manager.project_manager import ProjectManager
from opi.services.project import Project
from opi.services.project_env_vars import read_user_env_vars
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_store import ConflictError
from opi.services.step_up import StepUpAction
from opi.utils.age import get_decoded_project_private_key
from opi.utils.yaml_util import dump_yaml_to_string
from opi.web.task_progress import create_task_and_render_progress

logger = logging.getLogger(__name__)

env_vars_web_router = APIRouter(prefix="/projects", tags=["env-vars-web"])

#: The same converter and validator as the service's editable: one reader per field.
_CONVERTER = KeyValueConverter(fmt="env", write_as="string")
_VALIDATOR = KeyValueValidator()


def _scope_holder(project_data: dict[str, Any], component_name: str, deployment_name: str | None) -> dict[str, Any]:
    """The dict in the project file that carries the ``user-env-vars``, or a 404."""
    project = Project(project_data)
    if deployment_name is None:
        component = project.find("components", name=component_name)
        if component is not None:
            return component
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' niet gevonden")
    deployment = project.find("deployments", name=deployment_name)
    dep_component = Project.locate((deployment or {}).get("components") or [], reference=component_name)
    if dep_component is not None:
        return dep_component
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


async def _render_window(
    request: Request,
    project_name: str,
    component_name: str,
    deployment_name: str | None,
    *,
    vars_text: str,
    error: str = "",
) -> str:
    return render_fragment(
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
            "post_url": _window_url(project_name, component_name, deployment_name),
        },
    )


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
    holder = _scope_holder(project_data, component_name, deployment_name)

    where = _scope_label(component_name, deployment_name)
    try:
        private_key = await get_decoded_project_private_key(project_data)
        parsed = await read_user_env_vars(holder.get("user-env-vars"), private_key, where=where)
    except (KeyError, ValueError) as exc:
        logger.warning("Projectsleutel van %s niet te lezen voor %s: %s", project_name, where, exc)
        parsed = None

    if parsed is None:
        vars_text = ""
        error = "De opgeslagen variabelen zijn niet te lezen. Opslaan vervangt ze door wat je hier invult."
    else:
        # The converter formats the mapping into KEY=value lines; ``parsed`` is already
        # decrypted, so nothing is decrypted here.
        vars_text = _CONVERTER.read(parsed)
        error = ""
    return HTMLResponse(
        content=await _render_window(
            request, project_name, component_name, deployment_name, vars_text=vars_text, error=error
        )
    )


@env_vars_web_router.post(
    "/{project_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@action(Action.ENV_VARS_MANAGE)
@targets_in_query("deployment_name")
@env_vars_web_router.post(
    "/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars",
    response_class=HTMLResponse,
)
@requires_sso
@requires_step_up(action=StepUpAction.ENV_VARS_MANAGE, fields=("vars",))
async def save_env_vars(
    request: Request, project_name: str, component_name: str, deployment_name: str | None = None
) -> Response:
    """Write the variables of this scope and process the project again.

    The decorator checks the step-up again, so once the window has run out the OTP question
    follows instead of a save.
    """
    text = str((await request.form()).get("vars", ""))

    errors = _VALIDATOR.validate(text)
    if errors:
        return HTMLResponse(
            content=await _render_window(
                request, project_name, component_name, deployment_name, vars_text=text, error="; ".join(errors)
            )
        )

    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        project_data = await project_manager.get_contents()
        holder = _scope_holder(project_data, component_name, deployment_name)

        # ``write`` encrypts with the project key from the context; empty means the key is
        # removed, the same ``remove_when_none`` the editable declares.
        new_block = _CONVERTER.write(text, project_data)
        existing_block = holder.get("user-env-vars")
        if isinstance(new_block, str) and isinstance(existing_block, str):
            new_block = keep_existing_ciphertext_if_unchanged(existing_block, new_block, project_data)

        if new_block == existing_block or (new_block is None and existing_block is None):
            logger.info(
                "Omgevingsvariabelen van %s ongewijzigd, geen opslag", _scope_label(component_name, deployment_name)
            )
            return _unchanged_response(request, project_name)

        if new_block is None:
            holder.pop("user-env-vars", None)
        else:
            holder["user-env-vars"] = new_block

        try:
            await project_manager.save_and_commit_project(
                project_data, f"Omgevingsvariabelen van {_scope_label(component_name, deployment_name)}"
            )
        except (ProjectSchemaError, ProjectIntegrityError, ConflictError) as exc:
            logger.warning("Opslag van omgevingsvariabelen geweigerd voor %s: %s", project_name, exc)
            return HTMLResponse(
                content=await _render_window(
                    request, project_name, component_name, deployment_name, vars_text=text, error=str(exc)
                )
            )
    finally:
        await project_manager.close()

    # The same payload as the modal after its save: the file is already written, so the task
    # only processes.
    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="create_project",
        payload={
            "project_name": project_name,
            "yaml_content": dump_yaml_to_string(project_data),
            "deployment_name": None,
            "project_file_already_saved": True,
        },
        current_step=f"Omgevingsvariabelen van {_scope_label(component_name, deployment_name)} opgeslagen; project wordt verwerkt...",
        success_message=f"Omgevingsvariabelen van {_scope_label(component_name, deployment_name)} opgeslagen",
    )


def _unchanged_response(request: Request, project_name: str) -> Response:
    """Nothing changed: back to the components page, as a full navigation under htmx."""
    url = f"/projects/{project_name}/componenten"
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)
