"""Turning an invitation's stored destination into an address, and back.

An invitation's success button points somewhere. Two shapes may be stored (see
``config_model.InviteEntry``), and they live side by side with a fixed precedence:

1. ``application-target`` -- the CHOICE, as ``component:deployment[:/path]`` (see
   ``target_format``). The address is worked out here, at the moment the page is rendered,
   so it follows a subdomain, domain-format or cluster change instead of going stale.
2. ``application-url`` -- a fixed address. For a destination outside this project, and for
   every invitation that already stores one: those are NOT rewritten. An invitation is a
   standing arrangement with someone who already holds the link, so a migration that
   converts the destination silently changes where that person ends up, and on a wrong
   match ends them up somewhere else entirely.
3. Neither -- no button.

The derivation is not repeated here: it is ``public_urls_for_project``, the same one the
detail page lists and the picker offers. Two readers need it -- the form (through the
picker's converter) and the public page (through ``resolve_invite_url``) -- and the point
of this module is that they share one source rather than each growing their own.

A target that no longer resolves -- component removed, publish-on-web switched off,
deployment gone -- yields nothing, and the page shows NO button. That is the choice the
form already makes for the empty option: no button beats a button pointing somewhere
wrong, because the user cannot tell the difference until they have clicked it.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.invite.target_format import join_target, split_target

logger = logging.getLogger(__name__)


def derived_destinations(project_data: dict[str, Any]) -> list[dict[str, str]]:
    """Every public address of this project, one row per deployment/component/path.

    A half-configured project (no cluster yet, no domain chosen) yields an empty list
    rather than raising: this feeds a picker and a rendered page, and neither should
    break because one deployment is mid-configuration.

    Imported inside the function: ``project_file_handler`` imports the invite config
    model, so importing either at module level closes a cycle.
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.catalog.publish_on_web.urls import public_urls_for_project

    try:
        return public_urls_for_project(project_data, ProjectFileHandler())
    except KeyError, ValueError, AttributeError, TypeError:
        logger.debug("Could not derive the public addresses of this project", exc_info=True)
        return []


def url_for_target(target: str | None, project_data: dict[str, Any]) -> str | None:
    """The address this target points at today, or None if it no longer resolves."""
    component, deployment, path = split_target(target)
    if not component or not deployment:
        return None
    for row in derived_destinations(project_data):
        if row.get("component_name") != component or row.get("deployment_name") != deployment:
            continue
        #: No path stored means "the component's only address", so any row for that
        #: component answers. The path is only recorded where it distinguishes.
        if path is None or (row.get("path") or "/") == path:
            return row.get("url")
    return None


def target_for_url(url: str, project_data: dict[str, Any]) -> str | None:
    """The choice behind ``url``, as a stored value, or None if it is not one of ours.

    The path is only included where the component publishes more than one address, so the
    stored value carries exactly what it needs to be unambiguous and nothing else -- the
    same rule the picker's label follows.
    """
    rows = derived_destinations(project_data)
    match = next((row for row in rows if row.get("url") == url), None)
    if match is None:
        return None

    component = match.get("component_name") or ""
    deployment = match.get("deployment_name") or ""
    addresses = {
        row.get("url")
        for row in rows
        if row.get("component_name") == component and row.get("deployment_name") == deployment
    }
    path = (match.get("path") or "/") if len(addresses) > 1 else None
    return join_target(component, deployment, path)


def resolve_invite_url(invite: dict[str, Any], project_data: dict[str, Any]) -> str:
    """Where the success button of this invitation points, as an address for the template.

    ``invite`` is an entry as ``extract_invites_config`` normalizes it (underscore field
    names). Applies the precedence above and returns an empty string for "show no button",
    which is what the template already tests for.
    """
    target = invite.get("application_target")
    if target:
        url = url_for_target(target, project_data)
        if url is None:
            logger.info("Invite destination %r no longer resolves to a public address; showing no button", target)
            return ""
        return url
    return invite.get("application_url") or ""
