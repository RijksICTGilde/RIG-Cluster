"""Het endpoint dat het detailblok van deze dienst met clusterstatus vult.

Of de proxy klaar is staat in de ``Organization`` in het cluster en niet in het
projectbestand, en een renderend blok mag geen connector aanroepen; vandaar een
htmx-lazyload, dezelfde vorm als de backups.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso

logger = logging.getLogger(__name__)

SECTION_TEMPLATE = "image_registries/section-detail.html.j2"
STATUS_TEMPLATE = "image_registries/status-fragment.html.j2"

image_registries_router = APIRouter()


@image_registries_router.get("/projects/details/{project_name}/image-registries/status", response_class=HTMLResponse)
@requires_sso
async def registry_status_fragment(request: Request, project_name: str) -> HTMLResponse:
    """De toestand van de proxy-organisaties van dit project, opgehaald uit het cluster.

    Alleen zinvol op een cluster met de Quay-operator; elders is er geen organisatie en
    zegt het fragment dat er niets te wachten valt.
    """
    from opi.connectors.kubectl import KubectlConnector
    from opi.core.auth_decorators import get_current_user
    from opi.core.cluster_config import get_image_registries_config, get_prefixed_namespace
    from opi.core.config import settings
    from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY, project_registries
    from opi.services.project_authorization import is_user_authorized_for_project
    from opi.services.project_store import get_project_store
    from opi.web.lotc_switch import render

    user = get_current_user(request) or {}
    if not is_user_authorized_for_project(project_name, user.get("email", "").lower()):
        raise HTTPException(status_code=403, detail="Not authorized")

    cluster = settings.CLUSTER_MANAGER
    cluster_config = get_image_registries_config(cluster)
    if cluster_config.get("backend") != BACKEND_QUAY_PROXY:
        return render(request, template=STATUS_TEMPLATE, context={"applicable": False, "statuses": []})

    # ``get()`` en niet ``get_decrypted()``: dit fragment leest alleen ``name`` en ``upstream``.
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
    kubectl: Any, namespace: str, registry_name: str, upstream: str, customer_name: str, project_name: str
) -> dict[str, Any]:
    """Wat het cluster over een proxy-organisatie zegt, in de woorden van het scherm.

    Een organisatie die er nog niet is, is geen fout: ArgoCD maakt hem aan.
    """
    from opi.services.catalog.image_registries.naming import organization_name

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
    }


#: Hoeveel dagen voor het verlopen van het token de melding dringend wordt.
EXPIRY_WARNING_DAYS = 14


def _expires_soon(expires_at: str) -> bool:
    """Of het token binnen ``EXPIRY_WARNING_DAYS`` verloopt; een onleesbare datum niet."""
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
