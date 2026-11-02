"""De step-up bij een API-aanroep die productie raakt of het platform beheert (RC-332).

Sinds RC-377 vraagt ook elke wijzigende beheerroute er een; wat de aanleiding was staat in
``reason``, niet in ``detail``. Buiten die twee verandert er niets: de projectsleutel en de
bearer-tokens werken zoals ze werkten. Elke step-up is een verse factor van de AANROEPER zelf:
een persoonlijk token hoort bij een gebruiker, dus de code wordt getoetst tegen de factor van
de eigenaar van dat token. Er is geen manier om de code van iemand anders aan een aanroep te hangen.

Met de projectsleutel kan zo'n handeling niet: die hoort bij geen enkele gebruiker en een
machine heeft geen tweede factor. Dat is altijd een 403, ook met een code erbij.

Of de aanroeper de handeling MAG blijft hier buiten: dat is de autorisatie van de route
zelf, en met de eigenaar van het token als aanroeper is die al gesteld voordat dit draait.
De step-up antwoordt alleen op "is het echt hij, nu".

Geen venster zoals in het portaal: een API-aanroep draagt zijn eigen bewijs, per verzoek.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from opi.services.user_otp_service import OtpVerification, UserOtpService
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from opi.core.step_up import StepUpAction
    from starlette.requests import Request

logger = logging.getLogger(__name__)

#: De code uit de authenticator van de aanroeper. De enige header die hierbij hoort.
OTP_CODE_HEADER = "X-ZAD-OTP"

#: Waaraan een client (de zad-cli voorop) dit antwoord herkent en om een code vraagt.
STEP_UP_PROBLEM_TYPE = "urn:zad:step-up-required"

#: Waar een gebruiker zijn tweede factor inricht.
OTP_ENROLL_PATH = "/account/otp"

STEP_UP_PROBLEM_MEDIA_TYPE = "application/problem+json"


class StepUpCause(StrEnum):
    """Waarom deze aanroep werd geweigerd, zover een client dat mag weten.

    Genoeg om te weten wat te doen, nooit genoeg om een code te raden: ``CODE_INVALID``
    dekt zowel een verkeerde als een al gebruikte code, en zegt niet welk deel faalde.
    """

    OTP_MISSING = "otp-missing"
    FACTOR_MISSING = "factor-missing"
    CODE_INVALID = "code-invalid"
    BLOCKED = "blocked"
    PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"


class StepUpProblem(BaseModel):
    """Het 403-antwoord op een handeling die een verse tweede factor vraagt (RFC 7807).

    ``action``, ``reason`` en ``remedy`` zijn wat een client leest: welke handeling het
    was, waarom ze een step-up vraagt, en wat er moet gebeuren.
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
    otp_header: str = Field(default=OTP_CODE_HEADER, description="De header waarin de code hoort")
    enroll_url: str | None = Field(
        default=None, description="Waar de aanroeper zijn 2-factor inricht; alleen bij 'factor-missing'"
    )
    blocked_until: str | None = Field(
        default=None, description="Wanneer het weer mag, in ISO 8601; alleen bij 'geblokkeerd'"
    )


_DETAIL = {
    # Niet "raakt productie": sinds RC-377 vraagt ook platformbeheer een step-up. Waarom
    # DEZE aanroep er een vraagt staat in ``reason``.
    StepUpCause.OTP_MISSING: "Deze handeling vraagt een verse code uit je 2-factor.",
    StepUpCause.FACTOR_MISSING: "Je hebt nog geen 2-factor ingericht, en deze handeling vraagt er een.",
    StepUpCause.CODE_INVALID: "De meegestuurde code is niet aanvaard.",
    StepUpCause.BLOCKED: "Je 2-factor is tijdelijk geblokkeerd na te veel mislukte pogingen.",
    StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED: (
        "Deze handeling vraagt een verse 2-factor, en de sleutel waarmee je aanroept hoort bij geen gebruiker."
    ),
}

_REMEDY = {
    StepUpCause.OTP_MISSING: f"Stuur '{OTP_CODE_HEADER}' mee met een code uit je authenticator en herhaal de aanroep.",
    StepUpCause.FACTOR_MISSING: (
        f"Richt je 2-factor in op '{OTP_ENROLL_PATH}' en herhaal de aanroep met '{OTP_CODE_HEADER}'."
    ),
    StepUpCause.CODE_INVALID: f"Wacht op een nieuwe code in je authenticator en herhaal de aanroep met '{OTP_CODE_HEADER}'.",
    StepUpCause.BLOCKED: "Probeer het opnieuw zodra de blokkade voorbij is; 'blocked_until' zegt wanneer.",
    StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED: (
        "Met een projectsleutel kan dit niet: gebruik een persoonlijk token en stuur je eigen code mee."
    ),
}

#: Welke uitkomst van de verificatie bij welke reden hoort.
_CAUSE_PER_UITKOMST = {
    OtpVerification.NOT_REGISTERED: StepUpCause.FACTOR_MISSING,
    OtpVerification.LOCKED: StepUpCause.BLOCKED,
    OtpVerification.INVALID: StepUpCause.CODE_INVALID,
}


def _token_owner(request: Request) -> str | None:
    """Het e-mailadres van de eigenaar van het token, of None bij een projectsleutel.

    Op ``/api/``-paden zetten alleen de bearer-decorators en ``handhaaf`` (voor een
    persoonlijk token) ``request.state.user``; de sessiemiddleware leegt hem daar juist.
    Een gevulde waarde is dus een persoon.
    """
    user = getattr(request.state, "user", None) or {}
    return str(user.get("email") or "").strip().lower() or None


async def _cause(email: str | None, code: str) -> tuple[StepUpCause, str | None] | None:
    """Waarom deze aanroep geweigerd wordt, met het moment waarop het weer mag, of None."""
    if email is None:
        return StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED, None
    if not code:
        return StepUpCause.OTP_MISSING, None

    service = UserOtpService()
    uitkomst = await service.verify(email, code)
    if uitkomst is OtpVerification.OK:
        return None
    if uitkomst is OtpVerification.LOCKED:
        locked_until = (await service.status(email)).locked_until
        return StepUpCause.BLOCKED, locked_until.isoformat() if locked_until else None
    return _CAUSE_PER_UITKOMST[uitkomst], None


async def verify_api_step_up(
    request: Request,
    *,
    project_name: str,
    actie: StepUpAction,
    doelwit: str,
) -> StepUpProblem | None:
    """Toets de step-up van deze aanroep; geef ``None`` als hij door mag.

    Bij een weigering is de uitkomst het problem-detail dat de client terugkrijgt, zodat
    hij weet welke handeling het was, waarom ze een step-up vraagt en wat er moet gebeuren.
    """
    email = _token_owner(request)
    code = (request.headers.get(OTP_CODE_HEADER) or "").strip()

    uitkomst = await _cause(email, code)
    if uitkomst is None:
        logger.info(
            "Step-up geaccepteerd om %s: doel '%s', project '%s', door %s",
            actie.phrase,
            doelwit,
            project_name,
            email,
        )
        return None

    cause, blocked_until = uitkomst
    logger.warning(
        "Step-up geweigerd om %s: doel '%s', project '%s', door %s, reden %s",
        actie.phrase,
        doelwit,
        project_name,
        email or "een sleutel zonder gebruiker",
        cause.value,
    )
    return StepUpProblem(
        detail=_DETAIL[cause],
        instance=request.url.path,
        cause=cause,
        action=actie.value,
        reason=actie.reason(doelwit, project=project_name),
        remedy=_REMEDY[cause],
        enroll_url=OTP_ENROLL_PATH if cause is StepUpCause.FACTOR_MISSING else None,
        blocked_until=blocked_until,
    )


def step_up_problem_response(probleem: StepUpProblem) -> JSONResponse:
    """Het problem-detail als 403, met het mediatype dat RFC 7807 voorschrijft."""
    return JSONResponse(
        content=probleem.model_dump(mode="json"),
        status_code=403,
        media_type=STEP_UP_PROBLEM_MEDIA_TYPE,
    )
