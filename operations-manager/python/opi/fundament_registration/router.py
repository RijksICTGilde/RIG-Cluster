"""The registration endpoint of a Fundament installation: one POST, authenticated by a code.

The route recognises its caller itself, the way the sleep-mode router does: there is no
project and no API key here, only the shared fieldlab code, compared in constant time and
counted down in the limit on failed authentication. Everything under ``/api/`` already
skips the SSO check and CSRF.

The contract (fields, statuses, bodies) is in ``features/fundament-registratie.md`` and is
the agreement with the installer. A field that does not meet its rule is a 400 that names
the rule; the caller writes the installer and may read it.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opi.core.config import settings
from opi.fundament_registration import registration
from opi.fundament_registration.configuration import configuration, missing_settings, vlam_configuration
from opi.fundament_registration.registration import Answer, Outcome
from opi.http.rate_limiter import refuse_after_auth_failure, refuse_when_key_failures_exhausted
from opi.utils.patterns import DNS1123_LABEL

if TYPE_CHECKING:
    from opi.fundament_registration.store import Registration

logger = logging.getLogger(__name__)

fundament_router = APIRouter(prefix="/api/fundament", tags=["fundament"])

#: A DNS label: the team name becomes a subdomain, so it has to be one. The length is not in
#: the pattern, which is why it is checked next to it.
TEAM_PATTERN = re.compile(DNS1123_LABEL)
TEAM_MAX_LENGTH = 63

#: The cluster id travels in log lines and a database row, so it stays plain and bounded.
CLUSTER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: How long the installer waits before asking the same question again.
RETRY_AFTER_SECONDS = 10

_WRONG_CODE = "Ongeldige code"
_TEAM_IN_USE = "Deze teamnaam is al in gebruik, kies een andere"


@fundament_router.post("/registreer")
async def register(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Register a Fundament installation: set its DNS, obtain its certificate, hand both back.

    Answers 202 while the request is running, 200 once the certificate is there.
    """
    _authenticate(request, body.get("code"))
    missing = missing_settings()
    if missing:
        logger.error("fundament: de registratie staat aan maar mist instellingen: %s", ", ".join(missing))
        return _unavailable(f"Instellingen ontbreken: {', '.join(missing)}")

    team = _team(body.get("team"))
    cluster_id = _cluster_id(body.get("cluster_id"))
    ip = _address(body.get("ip"))

    answer = await registration.handle(team, cluster_id, ip, configuration())
    return _answer_as_response(answer)


def _authenticate(request: Request, presented: object) -> None:
    """Accept only the fieldlab code. A wrong one counts down the failure limit."""
    expected = settings.FUNDAMENT_REGISTRATION_CODE or ""
    code = presented if isinstance(presented, str) else ""
    refuse_when_key_failures_exhausted(code)
    # Compared on bytes: compare_digest on two str refuses a non-ASCII character with a
    # TypeError, and the code comes from outside.
    if not code or not hmac.compare_digest(code.encode(), expected.encode()):
        logger.warning("fundament: registratie geweigerd, de code klopt niet")
        refuse_after_auth_failure(request, code)
        raise HTTPException(status_code=401, detail=_WRONG_CODE)


def _team(value: object) -> str:
    # fullmatch, because ``$`` in the pattern lets a trailing newline through, and this value
    # becomes a primary key and a DNS name.
    if not isinstance(value, str) or len(value) > TEAM_MAX_LENGTH or not TEAM_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="team moet een DNS-label zijn: kleine letters, cijfers en streepjes, maximaal 63 tekens",
        )
    return value


def _cluster_id(value: object) -> str:
    if not isinstance(value, str) or not CLUSTER_ID_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="cluster_id moet 1 tot 128 tekens uit [A-Za-z0-9._-] zijn",
        )
    return value


def _address(value: object) -> str:
    """The public IPv4 address the domain points at."""
    try:
        address = ipaddress.IPv4Address(value if isinstance(value, str) else "")
    except ipaddress.AddressValueError as error:
        raise HTTPException(status_code=400, detail="ip moet een IPv4-adres zijn") from error
    # is_global already rules out private, loopback, link-local, CGNAT and reserved ranges;
    # a multicast address is global in that sense and so is refused separately.
    if not address.is_global or address.is_multicast:
        raise HTTPException(status_code=400, detail="ip moet een publiek IPv4-adres zijn")
    return str(address)


def _answer_as_response(answer: Answer) -> JSONResponse:
    if answer.outcome is Outcome.CLAIMED:
        raise HTTPException(status_code=409, detail=_TEAM_IN_USE)
    if answer.outcome is Outcome.FAILED:
        return _unavailable(answer.reason)
    if answer.outcome is Outcome.READY and answer.registration is not None:
        return _issued(answer.registration)
    return JSONResponse({"status": "bezig"}, status_code=202, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


def _issued(found: Registration) -> JSONResponse:
    """The finished registration, with the key in it, so this answer is never cached."""
    config = configuration()
    answer: dict[str, object] = {
        "domain": registration.domain_name(found.team, config.zone),
        "certificate": found.certificate or "",
        "private_key": found.private_key or "",
        "sso": {
            "discovery_url": config.sso_discovery_url,
            "client_id": config.sso_client_id,
            "client_secret": config.sso_client_secret,
        },
    }
    vlam = vlam_configuration()
    if vlam is not None:
        # Absent and not empty when this cluster has no VLAM: an installer that reads the
        # field can then see from its absence that there is nothing to configure.
        answer["vlam"] = {
            "login_server": vlam.login_server,
            "gateway": vlam.gateway,
            "api_host": vlam.api_host,
            "preauth_key": vlam.preauth_key,
        }
    return JSONResponse(answer, headers={"Cache-Control": "no-store"})


def _unavailable(reason: str) -> JSONResponse:
    """A 503 in the shape of the contract.

    Returned instead of raised: the handler for a 5xx in ``opi/server.py`` replaces the body
    of a raised one with its own envelope, and the installer reads ``detail``.
    """
    return JSONResponse({"detail": reason}, status_code=503, headers={"Retry-After": "30"})
