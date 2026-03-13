"""Federation health and peer status endpoints."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_master_api_key
from opi.connectors.opi import OpiConnector

logger = logging.getLogger(__name__)

federation_router: APIRouter = APIRouter(
    prefix="/api/federation",
    tags=["federation"],
    default_response_class=JSONResponse,
)


def _get_registry(request: Request):
    """Retrieve the federation registry from application state."""
    fed = getattr(request.app.state, "federation_service", None)
    if fed is None:
        return None
    return fed._registry


@federation_router.get("/peers")
@validate_master_api_key
async def list_peers(request: Request) -> JSONResponse:
    """Return the list of configured federation peers (no secrets)."""
    registry = _get_registry(request)
    if registry is None:
        return JSONResponse(content={"peers": [], "enabled": False})

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
@validate_master_api_key
async def federation_health(request: Request) -> JSONResponse:
    """Health-check each federation peer via its /health endpoint."""
    registry = _get_registry(request)
    if registry is None:
        return JSONResponse(content={"peers": [], "enabled": False})

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
            entry["status"] = str(exc)
            entry["healthy"] = False

        results.append(entry)

    return JSONResponse(content={"peers": results, "enabled": registry.is_enabled()})
