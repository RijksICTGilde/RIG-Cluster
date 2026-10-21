"""Helpers for keeping secrets out of logs.

Logging request/response headers is useful for debugging, but headers routinely
carry credentials (``Authorization: Bearer …``, ``X-API-Key``, cookies). Logging
them verbatim leaks live tokens into the log stream. Always run header dicts
through :func:`redact_sensitive_headers` before logging them.

Een geheim dat je wel moet kunnen VOLGEN door de log (een uitnodigingssleutel over een
reeks verzoeken) gaat door :func:`fingerprint_for_log`. Staat het geheim in een URL-PAD,
dan is dat :func:`redact_invite_key_in_path`.
"""

import hashlib
import re

# Header names (lower-cased) whose value must never be logged.
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "cookie",
        "set-cookie",
    }
)

_REDACTED = "***REDACTED***"


def redact_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential-bearing values masked.

    Matching is case-insensitive on the header name. Non-sensitive headers pass
    through unchanged so the log still shows the request shape.
    """
    return {key: (_REDACTED if str(key).lower() in _SENSITIVE_HEADERS else value) for key, value in headers.items()}


def fingerprint_for_log(value: str) -> str:
    """Een korte, niet terug te rekenen vingerafdruk van een geheim, voor de log.

    Acht hex-tekens van de SHA-256: genoeg om twee logregels over hetzelfde geheim aan
    elkaar te knopen, te weinig om het geheim zelf te zijn.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:8]


#: Het pad van de publieke uitnodigingsroutes. De sleutel is het segment achter
#: ``/invite/``; wat erachter staat (``/register``, ``/sso/callback``) blijft staan.
_INVITE_PATH = re.compile(r"(/invite/)([^/?\s]+)")


def redact_invite_key_in_path(path: str) -> str:
    """Vervang de uitnodigingssleutel in een URL-pad door zijn vingerafdruk.

    De sleutel is een bearer-geheim en staat in het pad, dus elke regel die een pad
    letterlijk logt zet hem in de log. De routes loggen hem zelf als vingerafdruk; dit is
    dezelfde omzetting voor elke regel die een heel pad schrijft, van de access-log van
    uvicorn tot de foutregels in de middleware.
    """
    return _INVITE_PATH.sub(lambda match: match.group(1) + fingerprint_for_log(match.group(2)), path)
