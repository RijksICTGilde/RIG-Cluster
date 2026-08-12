"""The dangerous project actions, addressed by key instead of by endpoint.

The project-details page confirms deleting and reprocessing in the same shared modal
as a service-contributed ``DeploymentAction``, and for the same reason: the page must
never say where a confirmation posts. It names the action by key; this module turns
that key plus the project's own data into the POST target, or into nothing at all.

That is the whole point of the indirection. An endpoint carried in the request would
be an open POST target, and one of these six endpoints deletes an entire project.
A target that is not a real deployment, component or attachment of this project never
produces an action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from opi.core.buttons import check_button_variant
from opi.handlers.project_file_handler import (
    COMPONENT_USAGE_WEB_ADDRESS,
    component_usage_sites,
    extract_attachment_catalog,
    extract_attachment_usage,
)

#: Confirmation button appearance per kind of action.
_DELETE_BUTTON = {"label": "Verwijderen", "icon": "verwijderen", "kind": "warning"}
_REFRESH_BUTTON = {"label": "Herverwerken", "icon": "refresh", "kind": "primary"}


@dataclass(frozen=True)
class ProjectAction:
    """A confirmed action on a project, its deployment, component or attachment.

    Shaped like ``DeploymentAction`` so both render through the same confirmation
    fragment: what a confirmation needs is a message, a button and an endpoint.
    """

    key: str
    label: str
    icon: str
    kind: str
    #: Web-route path the confirmation POSTs to; built here, never taken from the request.
    endpoint: str
    message: str
    #: Set when the action cannot be performed at all; the dialog then explains instead
    #: of offering a button (the server still refuses it on its own).
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        # ``kind`` wordt in het bevestigingsfragment een ``type`` op een <c-button>, en
        # het component slaat een variant die het niet kent stil over: de knop staat er
        # dan kaal bij. Zie opi/core/buttons.py.
        check_button_variant(self.kind, f"ProjectAction '{self.key}'")


def _display_name(project_name: str, project_data: dict[str, Any]) -> str:
    return str(project_data.get("display-name") or project_name)


def _names(entries: Any) -> list[str]:
    return [e.get("name") for e in entries or [] if isinstance(e, dict) and e.get("name")]


def build_project_action(
    project_name: str,
    project_data: dict[str, Any],
    action_key: str,
    target: str | None,
) -> ProjectAction | None:
    """The action with this key for this project, or None when it does not exist.

    ``target`` names the deployment, component or attachment the action is about; it is
    checked against the project's own contents, so an action can only ever address
    something this project really has.
    """
    project_path = quote(project_name, safe="")
    display = _display_name(project_name, project_data)

    if action_key == "refresh-project":
        if target:
            return None
        return ProjectAction(
            key=action_key,
            endpoint=f"/projects/{project_path}/refresh",
            message=(
                f'Weet u zeker dat u het project "{display}" wilt herverwerken? '
                "Dit synchroniseert alle deployments met het projectbestand. "
                "Verwijderde deployments worden opgeruimd."
            ),
            **_REFRESH_BUTTON,
        )

    if action_key == "delete-project":
        if target:
            return None
        return ProjectAction(
            key=action_key,
            endpoint=f"/projects/delete/{project_path}",
            message=(
                f'Weet u zeker dat u het volledige project "{display}" wilt verwijderen? '
                "Dit verwijdert ALLE deployments, services en configuratie."
            ),
            **_DELETE_BUTTON,
        )

    if action_key in ("refresh-deployment", "delete-deployment"):
        if not target or target not in _names(project_data.get("deployments")):
            return None
        deployment_path = quote(target, safe="")
        if action_key == "refresh-deployment":
            return ProjectAction(
                key=action_key,
                endpoint=f"/projects/{project_path}/refresh/{deployment_path}",
                message=(
                    f'Weet u zeker dat u deployment "{target}" wilt herverwerken? '
                    "Dit synchroniseert de manifesten voor deze deployment."
                ),
                **_REFRESH_BUTTON,
            )
        return ProjectAction(
            key=action_key,
            endpoint=f"/projects/{project_path}/delete-deployment/{deployment_path}",
            message=(
                f'Weet u zeker dat u deployment "{target}" wilt verwijderen? '
                "Dit verwijdert alle bijbehorende Kubernetes resources en ArgoCD configuratie."
            ),
            **_DELETE_BUTTON,
        )

    if action_key == "delete-component":
        if not target or target not in _names(project_data.get("components")):
            return None
        sites = component_usage_sites(project_data).get(target, [])
        web_addresses = [site for site in sites if site.kind == COMPONENT_USAGE_WEB_ADDRESS]
        # A component a deployment's web address is built around is refused by the delete
        # guard itself; say so here rather than offering a button that is going to be
        # refused. Other uses are removed along with it, which the message names, because
        # confirming a deletion means having seen what goes with it.
        blocked = (
            f"Dit component bepaalt het webadres van: {', '.join(site.label for site in web_addresses)}. "
            "Wijzig eerst het webadres daar; zolang dat zo staat kan dit component niet verwijderd worden."
            if web_addresses
            else None
        )
        message = f'Weet u zeker dat u component "{target}" wilt verwijderen?'
        if sites and not web_addresses:
            message += (
                f" Het wordt gebruikt door: {', '.join(site.label for site in sites)};"
                " die verwijzingen worden mee verwijderd."
            )
        return ProjectAction(
            key=action_key,
            endpoint=f"/projects/{project_path}/delete-component/{quote(target, safe='')}",
            message=message,
            blocked_reason=blocked,
            **_DELETE_BUTTON,
        )

    if action_key == "delete-attachment":
        catalog = extract_attachment_catalog(project_data)
        if not target or target not in catalog:
            return None
        entry = catalog[target]
        filename = entry.get("filename", target)
        used_by = extract_attachment_usage(project_data).get(target, [])
        # The catalog's own delete guard refuses an attachment that is still coupled;
        # say so here rather than offering a button that is going to be refused.
        blocked = (
            f"Deze bijlage is in gebruik door: {', '.join(used_by)}. "
            "Verwijder eerst de koppeling(en); zolang de bijlage in gebruik is kan deze niet verwijderd worden."
            if used_by
            else None
        )
        return ProjectAction(
            key=action_key,
            endpoint=f"/projects/{project_path}/attachments/{quote(target, safe='')}/delete",
            message=f'Weet u zeker dat u bijlage "{filename}" wilt verwijderen?',
            blocked_reason=blocked,
            **_DELETE_BUTTON,
        )

    return None
