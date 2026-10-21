"""Helpers for keeping secrets out of logs.

Logging request/response headers is useful for debugging, but headers routinely
carry credentials (``Authorization: Bearer …``, ``X-API-Key``, cookies). Logging
them verbatim leaks live tokens into the log stream. Always run header dicts
through :func:`redact_sensitive_headers` before logging them.

:class:`SecretRedactionFilter` vangt geheimen op in berichten van elke bibliotheek;
:func:`redact_text` doet hetzelfde voor tekst buiten logregels, zoals ``error_message``,
en :func:`redact_structure` voor de tekstvelden in een dict die naar de client gaat.

Een geheim dat je wel moet kunnen VOLGEN door de log (een uitnodigingssleutel over een
reeks verzoeken) gaat door :func:`fingerprint_for_log`. Staat het geheim in een URL-PAD,
dan is dat :func:`redact_invite_key_in_path`.
"""

import hashlib
import logging
import re
from typing import Any

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

# Dezelfde namen, maar zonder "cookie": dat woord staat ook in gewone logregels
# ("Failed to decode session cookie: <fout>") en zou daar de fouttekst opvreten. Een
# cookiewaarde in een header-dict loopt al door redact_sensitive_headers.
_HEADER_NAMES = "|".join(sorted(_SENSITIVE_HEADERS - {"cookie", "set-cookie"}, key=len, reverse=True))

# Elk patroon maskeert alleen de waarde, zodat de logregel zijn vorm houdt.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Een credential-header met zijn waarde, bijvoorbeeld "X-API-Key: abc123". Het schema
    # hoort bij de waarde, zodat "Authorization: Bearer x" een keer wordt gemaskeerd.
    # Basic staat erbij naast Bearer: zonder dat blijft de base64-credential naast het
    # merkteken staan, en dan meldt de regel een redactie die niet gebeurd is.
    (
        re.compile(rf"(?i)\b({_HEADER_NAMES})(['\"]?\s*[:=]\s*['\"]?)(?:(?:Bearer|Basic)\s+)?[^\s,;'\"}}]+"),
        rf"\1\2{_REDACTED}",
    ),
    # Een Bearer-token in vrije tekst. Minimaal 20 tekens, anders vangt dit ook een zin
    # als "Bearer authentication failed for route".
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]{20,}=*"), rf"\1{_REDACTED}"),
    # password=waarde, zoals in een URL, een commandoregel of een env-dump.
    (re.compile(r"(?i)(password\s*=\s*)[^\s,;&'\"]+"), rf"\1{_REDACTED}"),
    # "password": "waarde" in JSON. De sleutel moet gequoteerd zijn, anders vangt dit ook
    # "Failed to decrypt registry password: <fout>".
    (re.compile(r"(?i)(\"password\"\s*:\s*\")[^\"]*"), rf"\1{_REDACTED}"),
    # PASSWORD 'waarde' uit SQL: CREATE ROLE, ALTER ROLE en ALTER USER zetten het wachtwoord
    # zo neer, en ENCRYPTED PASSWORD valt onder hetzelfde patroon. De optionele = dekt
    # password='waarde', dat het patroon hierboven laat staan omdat de waarde gequoteerd is.
    # Dollar-quoting staat er niet bij: die vorm komt in deze codebase niet voor.
    # De twee quantifiers zijn possessief (``*+``): met gewone ``*`` is dit patroon
    # kwadratisch op "password" gevolgd door een lange witruimtereeks zonder ``'`` erna,
    # en dit filter draait synchroon op de event loop. Witruimte, ``=`` en ``'`` sluiten
    # elkaar uit, dus terugkrabbelen kan hier nooit een match opleveren.
    (re.compile(r"(?i)(password\s*+=?\s*+)'[^']*'"), rf"\1'{_REDACTED}'"),
    # Een AGE-privésleutel, waar hij ook in de regel staat.
    (re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]+"), f"AGE-SECRET-KEY-{_REDACTED}"),
)


def redact_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential-bearing values masked.

    Matching is case-insensitive on the header name. Non-sensitive headers pass
    through unchanged so the log still shows the request shape.
    """
    return {key: (_REDACTED if str(key).lower() in _SENSITIVE_HEADERS else value) for key, value in headers.items()}


def redact_text(text: str) -> str:
    """Maskeer bekende geheimvormen in een vrije tekst."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_structure(value: Any) -> Any:
    """Maskeer geheimvormen in elke tekenreeks binnen een dict, een lijst of een tekst.

    Voor het ``result``-veld van een taak: dat gaat als JSON naar de client en draagt
    dezelfde fouttekst als ``error_message``.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value


_EXCEPTION_FORMATTER = logging.Formatter()


class SecretRedactionFilter(logging.Filter):
    """Maskeert bekende geheimvormen in elk bericht, elke traceback en elke stack die een
    handler bereikt."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            # Bericht en args passen niet bij elkaar; formatter meldt de fout. De traceback
            # eronder hoort nog wel gemaskeerd te worden.
            message = ""

        redacted = redact_text(message)
        if redacted != message:
            # De args zitten al in het bericht, dus ze moeten weg: anders zet een tweede
            # formatteerronde het geheim terug.
            record.msg = redacted
            record.args = None

        if record.exc_info and not record.exc_text:
            # Zelf formatteren: de formatter doet dat pas na dit filter, en dan is een geheim
            # in het exception-bericht al in de log beland. Onze handlers gebruiken een
            # gewone Formatter, dus de uitvoer blijft dezelfde.
            record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


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
