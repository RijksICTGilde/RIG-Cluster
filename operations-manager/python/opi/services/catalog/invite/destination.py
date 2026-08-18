"""Turning an invitation's stored destination into an address, and back.

An invitation's success button points somewhere. Two shapes may be stored (see
``config_model.InviteEntry``):

* ``application-target`` -- the CHOICE: which deployment, which component, optionally
  which published path. The address is worked out here, at the moment the page is
  rendered, so it follows a subdomain, domain-format or cluster change instead of going
  stale.
* ``application-url`` -- a fixed address, for a destination outside this project (or one
  that predates the choice-shaped field and could not be matched back to a component).

The derivation is not repeated here: it is ``public_urls_for_project``, the same one the
detail page lists and the picker offers. Deriving it a second time is how two readers
came to disagree before (RC-104).

A target that no longer resolves -- component removed, publish-on-web switched off,
deployment gone -- yields nothing, and the page shows NO button. That is the choice the
form already makes for the empty option: no button beats a button pointing somewhere
wrong, because the user cannot tell the difference until they have clicked it.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.publish_on_web.urls import public_urls_for_project

logger = logging.getLogger(__name__)


def _handler() -> Any:
    """The project-file handler the URL derivation reads components through.

    Imported inside the function: ``project_file_handler`` imports the invite config
    model, so importing it at module level closes a cycle.
    """
    from opi.handlers.project_file_handler import ProjectFileHandler

    return ProjectFileHandler()


def derived_destinations(project_data: dict[str, Any]) -> list[dict[str, str]]:
    """Every public address of this project, one row per deployment/component/path.

    A half-configured project (no cluster yet, no domain chosen) yields an empty list
    rather than raising: this feeds a picker and a rendered page, and neither should
    break because one deployment is mid-configuration.
    """
    try:
        return public_urls_for_project(project_data, _handler())
    except KeyError, ValueError, AttributeError, TypeError:
        logger.debug("Could not derive the public addresses of this project", exc_info=True)
        return []


def _matches(row: dict[str, str], deployment: str, component: str, path: str | None) -> bool:
    if row.get("deployment_name") != deployment or row.get("component_name") != component:
        return False
    #: No path stored means "the component's only address", so any row for that component
    #: answers. Storing the path is only required where the component publishes several.
    return path is None or (row.get("path") or "/") == path


def url_for_target(target: dict[str, Any], project_data: dict[str, Any]) -> str | None:
    """The address this target points at today, or None if it no longer resolves."""
    deployment = target.get("deployment")
    component = target.get("component")
    if not deployment or not component:
        return None
    path = target.get("path")
    for row in derived_destinations(project_data):
        if _matches(row, str(deployment), str(component), path):
            return row.get("url")
    return None


def target_for_url(url: str, project_data: dict[str, Any]) -> dict[str, str] | None:
    """The deployment/component choice behind ``url``, or None if it is not one of ours.

    ``path`` is only included where the component publishes more than one address, so the
    stored record carries exactly what it needs to be unambiguous and nothing else -- the
    same rule the picker's label follows.
    """
    rows = derived_destinations(project_data)
    match = next((row for row in rows if row.get("url") == url), None)
    if match is None:
        return None

    deployment = match.get("deployment_name") or ""
    component = match.get("component_name") or ""
    target: dict[str, str] = {"deployment": deployment, "component": component}
    addresses = {
        row.get("url")
        for row in rows
        if row.get("deployment_name") == deployment and row.get("component_name") == component
    }
    if len(addresses) > 1:
        target["path"] = match.get("path") or "/"
    return target


def resolve_invite_url(invite: dict[str, Any], project_data: dict[str, Any]) -> str:
    """Where the success button of this invitation points, as an address for the template.

    ``invite`` is an entry as ``extract_invites_config`` normalizes it (underscore field
    names). Returns an empty string when there is no destination, or when a stored target
    no longer resolves -- both mean: show no button.
    """
    target = invite.get("application_target")
    if isinstance(target, dict):
        url = url_for_target(target, project_data)
        if url is None:
            logger.info(
                "Invite destination %s/%s no longer resolves to a public address; showing no button",
                target.get("deployment"),
                target.get("component"),
            )
            return ""
        return url
    return invite.get("application_url") or ""
