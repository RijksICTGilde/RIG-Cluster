"""De omgevingsvariabelen van een component bekijken en wijzigen (RC-388).

Ze staan niet meer op de weergavepagina: de detailpagina toont alleen hun namen en hun
aantal. Wie de waarden wil zien of wijzigen opent dit venster, en dat vraagt altijd een
verse step-up, ongeacht de OTAP-markering van de deployment. Binnen het venster van vijf
minuten leest en schrijft het formulier de echte waarden.

De generieke bewerkwizard en de componentmodals doen ``user-env-vars`` niet meer: geen
formulierhaak van de dienst grijpt het veld nog, dus een opslag langs die wegen laat de
opgeslagen waarde ongemoeid. De API-routes (values-endpoints) veranderen hier niet.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.auth_decorators import requires_sso, requires_step_up
from opi.core.authz.actions import Action, action, targets_in_query
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.core.step_up import StepUpAction
from opi.forms.editables.converters import KeyValueConverter, keep_existing_ciphertext_if_unchanged
from opi.forms.editables.validators import KeyValueValidator
from opi.manager.project_manager import ProjectManager
from opi.services.project_env_vars import read_user_env_vars
from opi.services.project_store import ConflictError
from opi.utils.age import unwrap_project_key
from opi.utils.yaml_util import dump_yaml_to_string
from opi.web.authz import authorized_project
from opi.web.lotc_switch import render_fragment
from opi.web.task_progress import create_task_and_render_progress

logger = logging.getLogger(__name__)

env_vars_web_router = APIRouter(prefix="/projects", tags=["env-vars-web"])

#: Dezelfde converter en validator als de editable van de dienst: een lezer per veld.
_CONVERTER = KeyValueConverter(fmt="env", write_as="string")
_VALIDATOR = KeyValueValidator()


def _scope_holder(project_data: dict[str, Any], component_name: str, deployment_name: str | None) -> dict[str, Any]:
    """Het dict in het projectbestand dat de ``user-env-vars`` draagt, of een 404."""
    if deployment_name is None:
        for component in project_data.get("components") or []:
            if isinstance(component, dict) and component.get("name") == component_name:
                return component
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' niet gevonden")
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict) or deployment.get("name") != deployment_name:
            continue
        for dep_component in deployment.get("components") or []:
            if isinstance(dep_component, dict) and dep_component.get("reference") == component_name:
                return dep_component
        break
    raise HTTPException(
        status_code=404, detail=f"Component '{component_name}' niet gevonden in deployment '{deployment_name}'"
    )


def _window_url(project_name: str, component_name: str, deployment_name: str | None) -> str:
    if deployment_name is None:
        return f"/projects/{project_name}/componenten/{component_name}/env-vars"
    return f"/projects/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars"


def _scope_label(component_name: str, deployment_name: str | None) -> str:
    """Voor log, commit en voortgang: de scope zonder de waarden."""
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
    fout: str = "",
) -> str:
    return render_fragment(
        request,
        template="bg/_env-vars-venster.html.j2",
        context={
            # request expliciet, net als in step_up_dialog: render_fragment voegt hem niet
            # zelf toe, en de template leest request.state.csrf_token voor het formulier.
            "request": request,
            "project_name": project_name,
            "component_name": component_name,
            "deployment_name": deployment_name,
            "vars_text": vars_text,
            "fout": fout,
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
@requires_step_up(actie=StepUpAction.ENV_VARS_MANAGE)
async def show_env_vars_window(
    request: Request, project_name: str, component_name: str, deployment_name: str | None = None
) -> HTMLResponse:
    """Het bewerkvenster met de echte waarden. Alleen na een verse step-up."""
    project = authorized_project(request)
    project_data = project.data or {}
    holder = _scope_holder(project_data, component_name, deployment_name)

    where = _scope_label(component_name, deployment_name)
    try:
        private_key = await unwrap_project_key(project_data["config"]["age-private-key"])
        parsed = await read_user_env_vars(holder.get("user-env-vars"), private_key, where=where)
    except (KeyError, ValueError) as exc:
        logger.warning("Projectsleutel van %s niet te lezen voor %s: %s", project_name, where, exc)
        parsed = None

    if parsed is None:
        vars_text = ""
        fout = "De opgeslagen variabelen zijn niet te lezen. Opslaan vervangt ze door wat je hier invult."
    else:
        # De converter formattereert de mapping naar KEY=value-regels; hij ontsleutelt
        # niets meer, want `parsed` is al ontsleuteld.
        vars_text = _CONVERTER.read(parsed)
        fout = ""
    return HTMLResponse(
        content=await _render_window(
            request, project_name, component_name, deployment_name, vars_text=vars_text, fout=fout
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
@requires_step_up(actie=StepUpAction.ENV_VARS_MANAGE, velden=("vars",))
async def save_env_vars(
    request: Request, project_name: str, component_name: str, deployment_name: str | None = None
) -> Response:
    """Schrijf de variabelen van deze scope en verwerk het project opnieuw.

    De step-up wordt hier opnieuw getoetst (de decorator), dus ook na het vijfminuten-
    venster volgt de OTP-vraag in plaats van een opslag.
    """
    text = str((await request.form()).get("vars", ""))

    errors = _VALIDATOR.validate(text)
    if errors:
        return HTMLResponse(
            content=await _render_window(
                request, project_name, component_name, deployment_name, vars_text=text, fout="; ".join(errors)
            )
        )

    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        project_data = await project_manager.get_contents()
        holder = _scope_holder(project_data, component_name, deployment_name)

        # ``write`` versleutelt met de projectsleutel uit de context; leeg betekent
        # "de sleutel gaat weg" (remove_when_none, zoals de editable dat verklaart).
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
                    request, project_name, component_name, deployment_name, vars_text=text, fout=str(exc)
                )
            )
    finally:
        await project_manager.close()

    # Dezelfde payload als de modal na zijn opslag (project_file_already_saved): het
    # bestand is al weggeschreven, de taak verwerkt alleen.
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
    """Niets veranderd: terug naar de componenten-pagina, onder htmx als volledige navigatie."""
    url = f"/projects/{project_name}/componenten"
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)
