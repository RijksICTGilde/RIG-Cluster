"""The ArgoCD state of ALL deployments of a project, in ONE query.

The deployment table on the Overview tab shows per row whether that deployment is healthy
and synced, so the LIST is fetched here: a single ``GET /api/v1/applications``, out of which
this project's applications are picked. One row more therefore costs nothing. The
applications of other projects do come over the wire and are thrown away here; that is the
price of one query instead of N.

This module delivers only the SUMMARY per deployment (health, sync, last sync, revision).
The full diagnosis, the error list and the namespace events, stays in the status card of the
opened panel: that asks more of ArgoCD and of kubectl than building a table should.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from opi.connectors.argo import create_argo_connector
from opi.utils.naming import generate_argocd_application_name

logger = logging.getLogger(__name__)

#: How long a fetched state is reused, in seconds. Short, because a stale "Healthy" is worse
#: than no status at all. What it catches is the BURST: opening the page, F5, switching tabs
#: and back, which are several renders within a few seconds. Anything beyond that fetches
#: again.
CACHE_TTL_SECONDS = 15

#: How long the page waits for ArgoCD before it goes on without a status column.
#:
#: This query sits on the render path and holds the project page up. A connect failure costs
#: nothing here, because the connector runs on budget 0; this limit is for an ArgoCD that
#: connects and then does not answer.
QUERY_TIMEOUT_SECONDS = 5

#: The fetched state per project: name -> (moment, state per deployment).
#:
#: In this process's memory, and so per OPI instance. That is enough: this catches a burst of
#: requests from the same user, not a shared truth between instances.
_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def clear_cache() -> None:
    """Throw the stored states away. For tests, and for whoever does not want the burst."""
    _cache.clear()


def _summarize(application: dict[str, Any]) -> dict[str, Any]:
    """The fields a table row needs, out of an ArgoCD application.

    The same reading as ``_fetch_argocd_deployment_status`` in the web router, so the row and
    the card in the panel do not use two different words for the same state.
    """
    status = application.get("status", {}) or {}
    sync = status.get("sync", {}) or {}
    operation_state = status.get("operationState", {}) or {}

    last_sync = operation_state.get("finishedAt")
    if not last_sync and sync.get("status") == "Synced":
        last_sync = status.get("reconciledAt")

    revision = sync.get("revision") or ""
    return {
        "available": True,
        "health": (status.get("health", {}) or {}).get("status", "Unknown"),
        "sync": sync.get("status", "Unknown"),
        "revision": revision[:7] if revision else None,
        "last_sync": last_sync,
    }


async def get_project_argocd_statuses(project_name: str, deployment_names: list[str]) -> dict[str, dict[str, Any]]:
    """The ArgoCD state of every named deployment, out of ONE query.

    Per deployment name a dict with ``available``, ``health``, ``sync``, ``revision`` and
    ``last_sync``. A deployment ArgoCD does not know IS in there, with ``available: False``,
    so the table can show the difference between "nothing rolled out yet" and "we do not
    know".

    With ArgoCD not connected an EMPTY dict comes back. The page knows that already
    (``argocd_available``) and says it there in plain words; inventing an "Unknown" per row
    here would shout over that.
    """
    if not deployment_names:
        return {}

    now = time.monotonic()
    stored = _cache.get(project_name)
    if stored is not None and now - stored[0] < CACHE_TTL_SECONDS:
        asked = set(deployment_names)
        # Only reuse when the stored state knows EVERY deployment asked for: with one added
        # since, the stored state is not the answer to this question.
        if asked <= set(stored[1]):
            return {name: stored[1][name] for name in deployment_names}

    # Fail fast, like every caller on a render path: the default budget would wait 30 seconds
    # with the page behind it.
    argo = create_argo_connector(connect_retry_seconds=0)
    # The same check the page itself does for ``argocd_available``, so the two cannot drift
    # apart: a page that says ArgoCD is connected should get states too, and the other way
    # round.
    if not await argo.ensure_authenticated():
        logger.debug("ArgoCD niet verbonden; geen gebundelde status voor %s", project_name)
        return {}

    # This query sits on the project page's RENDER PATH and holds the page up until it has an
    # answer, hence the limit around it: a slow or hanging ArgoCD costs a moment and yields a
    # table without a status column, and may not drag the page down with it.
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            applications = await argo.list_applications()
    except TimeoutError:
        logger.warning(
            "ArgoCD antwoordde niet binnen %ss; de deploymenttabel van %s krijgt geen statuskolom",
            QUERY_TIMEOUT_SECONDS,
            project_name,
        )
        return {}

    per_name = {
        name: application
        for application in applications
        if (name := (application.get("metadata", {}) or {}).get("name"))
    }

    states: dict[str, dict[str, Any]] = {}
    for deployment_name in deployment_names:
        app_name = generate_argocd_application_name(project_name, deployment_name)
        application = per_name.get(app_name)
        states[deployment_name] = (
            _summarize(application)
            if application is not None
            else {"available": False, "health": "Unknown", "sync": "Unknown", "revision": None, "last_sync": None}
        )

    _cache[project_name] = (now, states)
    # Drop the expired states of other projects at once, so this dict does not grow with the
    # number of projects ever looked at.
    for name in [name for name, (moment, _) in _cache.items() if now - moment >= CACHE_TTL_SECONDS]:
        del _cache[name]

    return states
