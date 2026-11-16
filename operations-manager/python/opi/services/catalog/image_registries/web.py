"""The endpoint that fills this service's detail block with cluster status.

Whether the proxy is ready lives in the ``Organization`` in the cluster and not in the project
file, and a rendering block may not call a connector; hence an htmx lazy load, the same shape
as the backups.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.connectors.kubectl import KubectlConnector
from opi.core.authz.actions import Action, action
from opi.core.cluster_config import get_image_registries_config, get_prefixed_namespace
from opi.core.config import settings
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import requires_sso
from opi.http.render.lotc_switch import render
from opi.services.catalog.image_registries.naming import organization_name
from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY, project_registries
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store

logger = logging.getLogger(__name__)

SECTION_TEMPLATE = "image_registries/section-detail.html.j2"
STATUS_TEMPLATE = "image_registries/status-fragment.html.j2"

image_registries_router = APIRouter()


@image_registries_router.get("/projects/details/{project_name}/image-registries/status", response_class=HTMLResponse)
@action(Action.SERVICE_READ)
@requires_sso
async def registry_status_fragment(request: Request, project_name: str) -> HTMLResponse:
    """The state of this project's proxy organizations, read from the cluster.

    Only meaningful on a cluster with the Quay operator; elsewhere there is no organization and
    the fragment says there is nothing to wait for.
    """
    user = get_current_user(request) or {}
    if not is_user_authorized_for_project(project_name, user.get("email", "").lower()):
        raise HTTPException(status_code=403, detail="Niet bevoegd")

    cluster = settings.CLUSTER_MANAGER
    cluster_config = get_image_registries_config(cluster)
    if cluster_config.get("backend") != BACKEND_QUAY_PROXY:
        return render(request, template=STATUS_TEMPLATE, context={"applicable": False, "statuses": []})

    # ``get()`` and not ``get_decrypted()``: this fragment reads only ``name`` and ``upstream``.
    project = get_project_store().get(project_name)
    project_data = project.data if project else None
    registries = project_registries(project_data or {})
    if not registries:
        return render(request, template=STATUS_TEMPLATE, context={"applicable": True, "statuses": []})

    namespace = get_prefixed_namespace(cluster, project_name)
    customer_name = cluster_config.get("customer_name", "")
    kubectl = KubectlConnector()

    statuses = [
        await _organization_status(
            kubectl,
            namespace,
            str(registry.get("name", "")),
            str(registry.get("upstream", "")),
            customer_name,
            project_name,
        )
        for registry in registries
        if registry.get("upstream")
    ]
    return render(request, template=STATUS_TEMPLATE, context={"applicable": True, "statuses": statuses})


async def _organization_status(
    kubectl: KubectlConnector, namespace: str, registry_name: str, upstream: str, customer_name: str, project_name: str
) -> dict[str, Any]:
    """What the cluster says about a proxy organization, in the words of the screen.

    An organization that is not there yet is not an error: ArgoCD creates it.
    """
    organization = organization_name(upstream, customer_name, project_name)
    stdout, stderr, code = await kubectl.run_command(
        ["get", "organization", organization, "-n", namespace, "-o", "json"]
    )
    if code != 0:
        logger.info(f"Organization '{organization}' nog niet gevonden in {namespace}: {stderr.strip()[:200]}")
        return {
            "registry": registry_name,
            "organization": organization,
            "state": "pending",
            "message": "De proxy wordt aangemaakt. Een pod die te vroeg start herstelt vanzelf.",
        }

    try:
        status = json.loads(stdout).get("status", {})
    except json.JSONDecodeError:
        logger.warning(f"Kon de status van organisatie '{organization}' niet lezen")
        return {"registry": registry_name, "organization": organization, "state": "unknown", "message": ""}

    ready = bool(status.get("proxyCache", {}).get("ready"))
    credentials = bool(status.get("credentialsConfigured"))
    expires = status.get("tokenExpiryDate") or ""
    if ready and credentials:
        message = "De proxy is klaar."
    elif ready:
        message = "De proxy draait, maar de inloggegevens zijn nog niet doorgekomen."
    else:
        message = "De proxy wordt klaargezet."
    return {
        "registry": registry_name,
        "organization": organization,
        "state": "ready" if (ready and credentials) else "pending",
        "message": message,
        "expires_at": expires,
        "expires_soon": _expires_soon(str(expires)),
        **_quota_usage(status.get("quota")),
    }


QUOTA_WARNING_PERCENT = 80  # Same as operator warningPercent


def _gib(value: object) -> str:
    """Bytes as gibibytes with one decimal, Dutch notation and without a trailing ``,0``."""
    gib = f"{int(str(value)) / 1024**3:.1f}".replace(".", ",")
    return gib.removesuffix(",0")


def _quota_usage(quota: object) -> dict[str, Any]:
    """The quota usage from ``status.quota``, or empty if the operator has not reported it yet."""
    if not isinstance(quota, dict):
        return {}
    try:
        percent = round(float(quota["usagePercent"]))
        used, limit = _gib(quota["usedBytes"]), _gib(quota["limitBytes"])
    except KeyError, TypeError, ValueError:
        logger.info(f"Onleesbare quotastand in de organisatiestatus: {quota}")
        return {}
    return {
        "quota_used_gib": used,
        "quota_limit_gib": limit,
        "quota_percent": percent,
        "quota_warning": percent >= QUOTA_WARNING_PERCENT,
    }


#: How many days before the token expires the message turns urgent.
EXPIRY_WARNING_DAYS = 14


def _expires_soon(expires_at: str) -> bool:
    """Whether the token expires within ``EXPIRY_WARNING_DAYS``; an unreadable date does not."""
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(expires_at)
    except ValueError:
        logger.info(f"Onleesbare tokenverloopdatum '{expires_at}'; geen waarschuwing")
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment - datetime.now(UTC) <= timedelta(days=EXPIRY_WARNING_DAYS)
