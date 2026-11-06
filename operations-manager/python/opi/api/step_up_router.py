"""The step-up of an API caller: one call with a code, then a window, like in the portal."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.otp_step_up import STEP_UP_PATH, step_up_problem_response, verify_step_up_code
from opi.api.params import ProjectNamePath
from opi.api.user_token_auth import validate_user_token
from opi.core.authz.actions import Action, action
from opi.services.api_step_up_service import ApiStepUpService
from opi.services.audit import record_step_up_attempt
from opi.utils.client_ip import client_ip
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

step_up_api_router: APIRouter = APIRouter(prefix="/api/v2", tags=["step-up"])


class StepUpRequest(BaseModel):
    """The code that activates a step-up."""

    code: str = Field(..., description="A code from the authenticator of the caller")


async def _activate(request: Request, code: str, *, project_name: str = "") -> Response:
    email = request.state.user["email"]
    credential = request.state.step_up_credential
    if not credential:
        logger.warning("Step-up refused for %s: the token has no sid claim", email)
        raise HTTPException(status_code=400, detail="Dit token heeft geen SSO-sessie om een step-up aan te hangen")

    probleem = await verify_step_up_code(request, code, project_name=project_name)
    if probleem is not None:
        await record_step_up_attempt(email, allowed=False, cause=probleem.cause.value, origin=client_ip(request))
        return step_up_problem_response(probleem)

    valid_until = await ApiStepUpService().record(credential, email)
    await record_step_up_attempt(email, allowed=True, cause=None, origin=client_ip(request))
    logger.info("Step-up via de API geactiveerd door %s", email)
    return JSONResponse({"valid_until": valid_until.isoformat()})


async def _end(request: Request) -> Response:
    if request.state.step_up_credential:
        await ApiStepUpService().clear(request.state.step_up_credential)
    logger.info("Step-up via de API beëindigd door %s", request.state.user["email"])
    return Response(status_code=204)


@step_up_api_router.post(STEP_UP_PATH.removeprefix("/api/v2"))
@action(Action.STEP_UP_VERIFY)
@validate_user_token
async def activate_step_up(request: Request, body: StepUpRequest) -> Response:
    """Activate a step-up for this SSO session.

    Until ``valid_until`` a handling that asks for a step-up needs no code, and a platform
    admin holds their rights. The window does not slide along with use.
    """
    return await _activate(request, body.code)


@step_up_api_router.delete(STEP_UP_PATH.removeprefix("/api/v2"), status_code=204)
@action(Action.STEP_UP_VERIFY)
@validate_user_token
async def end_step_up(request: Request) -> Response:
    """End the step-up of this SSO session before its window runs out."""
    return await _end(request)


@step_up_api_router.post("/projects/{project_name}/step-up")
@action(Action.TOKEN_STEP_UP)
@validate_api_token
async def activate_token_step_up(request: Request, project_name: ProjectNamePath, body: StepUpRequest) -> Response:
    """Activate a step-up for this personal token. A service account has no code to give."""
    return await _activate(request, body.code, project_name=project_name)


@step_up_api_router.delete("/projects/{project_name}/step-up", status_code=204)
@action(Action.TOKEN_STEP_UP)
@validate_api_token
async def end_token_step_up(request: Request, project_name: ProjectNamePath) -> Response:
    """End the step-up of this personal token before its window runs out."""
    return await _end(request)
