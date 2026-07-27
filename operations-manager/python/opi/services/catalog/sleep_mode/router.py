"""The sleep-mode API router: the two endpoints the waker pod calls.

Auth is a per-deployment wake token in ``X-Wake-Token`` (verified inside ``flow``
against the deployment's own stored token), deliberately NOT the project API key
(``validate_api_token``): a token leaking from a public waker pod may wake exactly one
deployment and nothing else. The token cannot be verified without loading the project,
so verification lives in ``flow`` rather than a header-only decorator.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode.flow import DeploymentNotFound, InvalidWakeToken

sleep_mode_router = APIRouter(prefix="/api/sleep-mode", tags=["sleep-mode"])


def _require_token(request: Request) -> str:
    token = request.headers.get("X-Wake-Token")
    if not token:
        raise HTTPException(status_code=401, detail="X-Wake-Token header required")
    return token


@sleep_mode_router.post("/{project_name}/{deployment_name}/wake")
async def wake(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """Start waking a deployment. 202 when a transition starts, 200 for a no-op."""
    token = _require_token(request)
    try:
        result = await flow.wake(project_name, deployment_name, presented_token=token)
    except InvalidWakeToken as exc:
        raise HTTPException(status_code=401, detail="Invalid wake token") from exc
    except DeploymentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"state": result.state}, status_code=202 if result.changed else 200)


@sleep_mode_router.get("/{project_name}/{deployment_name}/status")
async def status(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """Report whether the app behind the waker is back yet: starting | ready."""
    token = _require_token(request)
    try:
        result = await flow.status(project_name, deployment_name, presented_token=token)
    except InvalidWakeToken as exc:
        raise HTTPException(status_code=401, detail="Invalid wake token") from exc
    except DeploymentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result)
