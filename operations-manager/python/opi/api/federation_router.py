"""Federation health and peer status endpoints."""

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opi.connectors.opi import OpiConnector
from opi.core.authz.actions import Action, action
from opi.http.auth.user_token_auth import validate_platform_admin_token

if TYPE_CHECKING:
    from opi.core.federation_config import FederationRegistry

logger = logging.getLogger(__name__)

federation_router: APIRouter = APIRouter(
    prefix="/api/federation",
    tags=["federation"],
    default_response_class=JSONResponse,
)


def _registry_of_404(request: Request) -> FederationRegistry:
    """This instance's federation registry; raises 404 when federation is switched off.

    Without federation there is nothing here to report on, which is a route that does not
    exist on this cluster rather than a server error.
    """
    fed = getattr(request.app.state, "federation_service", None)
    registry = getattr(fed, "_registry", None)
    if registry is None or not registry.is_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return registry


@federation_router.get("/peers")
@action(Action.PLATFORM_FEDERATION)
@validate_platform_admin_token
async def list_peers(request: Request) -> JSONResponse:
    """Return the list of configured federation peers (no secrets)."""
    registry = _registry_of_404(request)
    peers = [
        {
            "cluster": p.cluster,
            "url": p.url,
            "verify_tls": p.verify_tls,
            "timeout": p.timeout,
        }
        for p in registry.get_all_peers()
    ]
    return JSONResponse(content={"peers": peers, "enabled": registry.is_enabled()})


@federation_router.get("/health")
@action(Action.PLATFORM_FEDERATION)
@validate_platform_admin_token
async def federation_health(request: Request) -> JSONResponse:
    """Health-check each federation peer via its /health endpoint."""
    registry = _registry_of_404(request)
    results = []
    for peer in registry.get_all_peers():
        entry: dict = {"cluster": peer.cluster, "url": peer.url}
        try:
            async with OpiConnector(
                base_url=peer.url,
                api_key=peer.api_key,
                verify_tls=peer.verify_tls,
                timeout=min(peer.timeout, 10),
            ) as conn:
                resp = await conn.health_check()
                entry["status"] = resp.get("status", "ok")
                entry["healthy"] = True
        except Exception as exc:
            # foutmelding-gewogen: the error of a peer next to that peer's own url, which only
            # a platform admin sees, so this hands the reader nothing they did not have.
            # Without the reason, "peer does not answer" is a health check without value.
            entry["status"] = str(exc)
            entry["healthy"] = False

        results.append(entry)

    return JSONResponse(content={"peers": results, "enabled": registry.is_enabled()})
