"""Federation health and peer status endpoints."""

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.user_token_auth import validate_platform_admin_token
from opi.connectors.opi import OpiConnector

if TYPE_CHECKING:
    from opi.core.federation_config import FederationRegistry

logger = logging.getLogger(__name__)

federation_router: APIRouter = APIRouter(
    prefix="/api/federation",
    tags=["federation"],
    default_response_class=JSONResponse,
)


def _registry_of_404(request: Request) -> FederationRegistry:
    """De federatieregistry van deze instantie, of een 404 als de federatie uit staat.

    Zonder federatie bestaat er hier niets om over te rapporteren, en dat is geen
    serverfout maar een route die op dit cluster niet bestaat.
    """
    fed = getattr(request.app.state, "federation_service", None)
    registry = getattr(fed, "_registry", None)
    if registry is None or not registry.is_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return registry


@federation_router.get("/peers")
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
            # foutmelding-gewogen: dit zet de fout van een peer naast de url van diezelfde
            # peer, die een beheerder zelf heeft ingevoerd, en alleen een platformbeheerder
            # ziet het. Er komt hier dus niets naar buiten dat de lezer niet al had; en
            # zonder de reden is "peer meldt zich niet" een health-check zonder waarde.
            entry["status"] = str(exc)
            entry["healthy"] = False

        results.append(entry)

    return JSONResponse(content={"peers": results, "enabled": registry.is_enabled()})
