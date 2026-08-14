"""The sleep-mode API router: waking a deployment, and asking whether it is back.

Two ways in, because there are two callers with genuinely different rights:

* ``X-Wake-Token`` -- the per-deployment token the waker pod carries. It is verified
  inside ``flow`` against the deployment's own stored token, so a token leaking from a
  public waker pod may wake exactly one deployment and nothing else. Verification cannot
  happen in a header-only check because it needs the project loaded.
* ``X-API-Key`` -- the project's own API key, checked here against the project in the URL.
  A project owner may wake his own deployment, and until now he could not: the endpoints
  demanded a wake token that no API and no page hands out (zad-cli, punt 4), so the two
  CLI commands were unusable. A key belonging to ANOTHER project is refused, exactly as
  everywhere else in the API.

Both headers are declared as parameters so they appear in the OpenAPI document; a
generated client can see what it may send instead of discovering a 401.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode.flow import DeploymentNotFound, InvalidWakeToken
from opi.services.project_store import get_project_store

sleep_mode_router = APIRouter(prefix="/api/sleep-mode", tags=["sleep-mode"])

WakeTokenHeader = Annotated[
    str | None,
    Header(
        alias="X-Wake-Token",
        description="The deployment's own wake token, as carried by its waker page. Wakes only this deployment.",
    ),
]
ApiKeyHeader = Annotated[
    str | None,
    Header(
        alias="X-API-Key",
        description="The project's API key. Accepted here as well, so a project owner can wake his own deployment.",
    ),
]


def _presented_token(project_name: str, wake_token: str | None, api_key: str | None) -> str | None:
    """Authenticate the caller and return what ``flow`` must still verify.

    A wake token is returned as-is: only the project file can say whether it belongs to
    this deployment. A valid project key needs nothing further, so None is returned --
    the same value the session-authenticated web route passes.
    """
    if wake_token:
        return wake_token
    if api_key:
        project = get_project_store().get(project_name)
        if project is None or not project.api_key or not secrets.compare_digest(project.api_key, api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")
        return None
    raise HTTPException(status_code=401, detail="X-Wake-Token or X-API-Key header required")


@sleep_mode_router.post("/{project_name}/{deployment_name}/wake")
async def wake(
    project_name: str,
    deployment_name: str,
    x_wake_token: WakeTokenHeader = None,
    x_api_key: ApiKeyHeader = None,
) -> JSONResponse:
    """Start waking a deployment. 202 when a transition starts, 200 for a no-op.

    Authenticate with the deployment's ``X-Wake-Token`` or with the project's ``X-API-Key``.
    """
    token = _presented_token(project_name, x_wake_token, x_api_key)
    try:
        result = await flow.wake(project_name, deployment_name, presented_token=token)
    except InvalidWakeToken as exc:
        raise HTTPException(status_code=401, detail="Invalid wake token") from exc
    except DeploymentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse({"state": result.state}, status_code=202 if result.changed else 200)


@sleep_mode_router.get("/{project_name}/{deployment_name}/status")
async def status(
    project_name: str,
    deployment_name: str,
    x_wake_token: WakeTokenHeader = None,
    x_api_key: ApiKeyHeader = None,
) -> JSONResponse:
    """Report whether the app behind the waker is back yet: starting | ready.

    Authenticate with the deployment's ``X-Wake-Token`` or with the project's ``X-API-Key``.
    """
    token = _presented_token(project_name, x_wake_token, x_api_key)
    try:
        result = await flow.status(project_name, deployment_name, presented_token=token)
    except InvalidWakeToken as exc:
        raise HTTPException(status_code=401, detail="Invalid wake token") from exc
    except DeploymentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result)
