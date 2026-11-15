"""The step-up on an API call that touches production or administers the platform (RC-332).

Since RC-377 every modifying admin route asks for one too; what the occasion was sits in
``reason``, not in ``detail``. Every step-up is a fresh factor of the CALLER themselves: a
personal token belongs to a user, so the code is checked against that token owner's factor.
There is no way to hang somebody else's code on a call.

With the project key such an action is impossible: it belongs to no user and a machine has
no second factor. That is always a 403, even with a code alongside.

Whether the caller MAY do the action stays out of here: that is the route's own
authorisation, and with the token's owner as the caller it has already been settled before
this runs. The step-up only answers "is it really them, now".

The same window as in the portal, since RC-431: a call activates the step-up
(``opi/api/step_up_router.py``) and every call after it carries it until the window is up.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from opi.services.step_up import StepUpAction
from opi.services.user_otp_service import OtpVerification, UserOtpService

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

#: Where an SSO token activates its step-up. A personal token does that per project.
STEP_UP_PATH = "/api/v2/step-up"

#: What a client (the zad-cli first of all) recognises this answer by, to ask for a code.
STEP_UP_PROBLEM_TYPE = "urn:zad:step-up-required"

#: Where a user sets up their second factor.
OTP_ENROLL_PATH = "/account/otp"

STEP_UP_PROBLEM_MEDIA_TYPE = "application/problem+json"


class StepUpCause(StrEnum):
    """Why this call was refused, as far as a client may know.

    Enough to know what to do, never enough to guess a code: ``CODE_INVALID`` covers both a
    wrong and an already used code, and does not say which part failed.
    """

    OTP_MISSING = "otp-missing"
    FACTOR_MISSING = "factor-missing"
    CODE_INVALID = "code-invalid"
    BLOCKED = "blocked"
    PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"


class StepUpProblem(BaseModel):
    """The 403 answer to an action that asks for a fresh second factor (RFC 7807).

    ``action``, ``reason`` and ``remedy`` are what a client reads: which action it was, why
    it asks for a step-up, and what has to happen.
    """

    type: str = Field(default=STEP_UP_PROBLEM_TYPE, description="Altijd 'urn:zad:step-up-required'")
    title: str = Field(default="Step-up vereist", description="Korte samenvatting van het fouttype")
    status: int = Field(default=403, description="HTTP-statuscode, herhaald zoals RFC 7807 voorschrijft")
    detail: str = Field(..., description="Wat er misging, in gewone taal; nooit welk deel van een code fout was")
    instance: str | None = Field(default=None, description="Het pad waarop de weigering optrad")
    cause: StepUpCause = Field(..., description="Waarom er werd geweigerd, als waarde waarop een client kan sturen")
    action: str = Field(..., description="De handeling die een step-up vraagt, bijvoorbeeld 'deployment-delete'")
    reason: str = Field(..., description="Waarom deze handeling er een vraagt, bijvoorbeeld dat ze productie raakt")
    remedy: str = Field(..., description="Wat de aanroeper moet doen om de handeling alsnog uit te voeren")
    enroll_url: str | None = Field(
        default=None, description="Waar de aanroeper zijn 2-factor inricht; alleen bij 'factor-missing'"
    )
    blocked_until: str | None = Field(
        default=None, description="Wanneer het weer mag, in ISO 8601; alleen bij 'geblokkeerd'"
    )


_DETAIL = {
    # Not "touches production": since RC-377 platform administration asks for a step-up as
    # well. Why THIS call asks for one sits in ``reason``.
    StepUpCause.OTP_MISSING: "Deze handeling vraagt een verse step-up met je 2-factor.",
    StepUpCause.FACTOR_MISSING: "Je hebt nog geen 2-factor ingericht, en deze handeling vraagt er een.",
    StepUpCause.CODE_INVALID: "De code is niet aanvaard.",
    StepUpCause.BLOCKED: "Je 2-factor is tijdelijk geblokkeerd na te veel mislukte pogingen.",
    StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED: (
        "Deze handeling vraagt een verse 2-factor, en de sleutel waarmee je aanroept hoort bij geen gebruiker."
    ),
}

_ACTIVATE = (
    f"POST {STEP_UP_PATH} met een SSO-token, of POST /api/v2/projects/{{project}}/step-up met een persoonlijk token"
)

_REMEDY = {
    StepUpCause.OTP_MISSING: f"Activeer een step-up met je code ({_ACTIVATE}) en herhaal de aanroep.",
    StepUpCause.FACTOR_MISSING: f"Richt je 2-factor in op '{OTP_ENROLL_PATH}' en activeer daarna een step-up.",
    StepUpCause.CODE_INVALID: "Wacht op een nieuwe code in je authenticator en probeer het opnieuw.",
    StepUpCause.BLOCKED: "Probeer het opnieuw zodra de blokkade voorbij is; 'blocked_until' zegt wanneer.",
    StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED: (
        "Met een projectsleutel of service account kan dit niet: gebruik een persoonlijk token en activeer een step-up."
    ),
}

#: Which outcome of the verification belongs to which cause.
_CAUSE_PER_OUTCOME = {
    OtpVerification.NOT_REGISTERED: StepUpCause.FACTOR_MISSING,
    OtpVerification.LOCKED: StepUpCause.BLOCKED,
    OtpVerification.INVALID: StepUpCause.CODE_INVALID,
}


def _token_owner(request: Request) -> str | None:
    """The email address of the token's owner, or None on a project key.

    On ``/api/`` paths only the bearer decorators and ``enforce`` (for a personal token) set
    ``request.state.user``; the session middleware empties it there. A filled value is
    therefore a person.
    """
    user = getattr(request.state, "user", None) or {}
    return str(user.get("email") or "").strip().lower() or None


async def _cause(email: str | None, code: str) -> tuple[StepUpCause, str | None] | None:
    """Why this call is refused, with the moment it is allowed again, or None."""
    if email is None:
        return StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED, None
    if not code:
        return StepUpCause.OTP_MISSING, None

    service = UserOtpService()
    outcome = await service.verify(email, code)
    if outcome is OtpVerification.OK:
        return None
    if outcome is OtpVerification.LOCKED:
        locked_until = (await service.status(email)).locked_until
        return StepUpCause.BLOCKED, locked_until.isoformat() if locked_until else None
    return _CAUSE_PER_OUTCOME[outcome], None


async def verify_api_step_up(
    request: Request,
    *,
    project_name: str,
    action: StepUpAction,
    target: str,
) -> StepUpProblem | None:
    """Check whether this caller has a fresh step-up; give ``None`` when they may pass.

    On a refusal the outcome is the problem detail the client gets back, so that it knows
    which action it was, why it asks for a step-up and what has to happen.
    """
    email = _token_owner(request)
    if email is not None and getattr(request.state, "step_up_fresh", False) is True:
        logger.info(
            "Verse step-up gebruikt om %s: doel '%s', project '%s', door %s",
            action.phrase,
            target,
            project_name,
            email,
        )
        return None
    cause = StepUpCause.OTP_MISSING if email is not None else StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED
    return _problem(request, cause, None, project_name=project_name, action=action, target=target)


async def verify_step_up_code(request: Request, code: str, *, project_name: str = "") -> StepUpProblem | None:
    """Check the code of the step-up call; give ``None`` when it is right, else the problem detail."""
    email = _token_owner(request)
    outcome = await _cause(email, code.strip())
    if outcome is None:
        return None
    cause, blocked_until = outcome
    return _problem(
        request, cause, blocked_until, project_name=project_name, action=StepUpAction.ELEVATE, target=email or ""
    )


def _problem(
    request: Request,
    cause: StepUpCause,
    blocked_until: str | None,
    *,
    project_name: str,
    action: StepUpAction,
    target: str,
) -> StepUpProblem:
    logger.warning(
        "Step-up geweigerd om %s: doel '%s', project '%s', door %s, reden %s",
        action.phrase,
        target,
        project_name,
        _token_owner(request) or "een sleutel zonder gebruiker",
        cause.value,
    )
    return StepUpProblem(
        detail=_DETAIL[cause],
        instance=request.url.path,
        cause=cause,
        action=action.value,
        reason=action.reason(target, project=project_name),
        remedy=_REMEDY[cause],
        enroll_url=OTP_ENROLL_PATH if cause is StepUpCause.FACTOR_MISSING else None,
        blocked_until=blocked_until,
    )


#: The 403 as it appears in the OpenAPI, here and not per route: there is then one
#: description of this answer, for the routes that impose it (``opi/server.py``) and for the
#: step-up call that returns it itself (``opi/api/step_up_router.py``).
STEP_UP_403_RESPONSE: dict[str, object] = {
    "description": (
        "Deze handeling vraagt een verse 2-factor. Het antwoord heeft type "
        f"'{STEP_UP_PROBLEM_TYPE}'; 'cause' zegt waarom er werd geweigerd en 'remedy' wat er "
        f"moet gebeuren. Activeer een step-up met '{STEP_UP_PATH}' en herhaal de aanroep."
    ),
    "content": {STEP_UP_PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/StepUpProblem"}}},
}


def step_up_problem_response(problem: StepUpProblem) -> JSONResponse:
    """The problem detail as a 403, with the media type RFC 7807 prescribes."""
    return JSONResponse(
        content=problem.model_dump(mode="json"),
        status_code=403,
        media_type=STEP_UP_PROBLEM_MEDIA_TYPE,
    )
