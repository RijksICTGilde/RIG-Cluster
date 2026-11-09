"""Helpers for keeping secrets out of logs.

Logging request/response headers is useful for debugging, but headers routinely
carry credentials (``Authorization: Bearer …``, ``X-API-Key``, cookies). Logging
them verbatim leaks live tokens into the log stream. Always run header dicts
through :func:`redact_sensitive_headers` before logging them.

:class:`SecretRedactionFilter` catches secrets in the messages of any library;
:func:`redact_text` does the same for text outside log records, such as ``error_message``,
and :func:`redact_structure` for the text fields of a dict that goes to the client.

A secret you do have to FOLLOW through the log (an invite key across a series of requests)
goes through :func:`fingerprint_for_log`. When the secret sits in a URL PATH, that is
:func:`redact_invite_key_in_path`.
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

# The same names, but without "cookie": that word also appears in ordinary log records
# ("Failed to decode session cookie: <error>") where it would eat the error text. A cookie
# value in a header dict already passes through redact_sensitive_headers.
_HEADER_NAMES = "|".join(sorted(_SENSITIVE_HEADERS - {"cookie", "set-cookie"}, key=len, reverse=True))

# Every pattern masks the value only, so the log record keeps its shape.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A credential header with its value, for instance "X-API-Key: abc123". The scheme
    # belongs to the value, so that "Authorization: Bearer x" is masked once. Basic sits
    # beside Bearer: without it the base64 credential stays next to the marker, and the
    # record then reports a redaction that did not happen.
    (
        re.compile(rf"(?i)\b({_HEADER_NAMES})(['\"]?\s*[:=]\s*['\"]?)(?:(?:Bearer|Basic)\s+)?[^\s,;'\"}}]+"),
        rf"\1\2{_REDACTED}",
    ),
    # A Bearer token in free text. At least 20 characters, otherwise this also catches a
    # sentence such as "Bearer authentication failed for route".
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]{20,}=*"), rf"\1{_REDACTED}"),
    (re.compile(r"(?i)(password\s*=\s*)[^\s,;&'\"]+"), rf"\1{_REDACTED}"),
    # "password": "value" in JSON. The key has to be quoted, otherwise this also catches
    # "Failed to decrypt registry password: <error>".
    (re.compile(r"(?i)(\"password\"\s*:\s*\")[^\"]*"), rf"\1{_REDACTED}"),
    # PASSWORD 'value' from SQL: CREATE ROLE, ALTER ROLE and ALTER USER write the password
    # that way, and ENCRYPTED PASSWORD falls under the same pattern. The optional = covers
    # password='value', which the pattern above leaves alone because the value is quoted.
    # The two quantifiers are possessive (``*+``): with a plain ``*`` this pattern is
    # quadratic on "password" followed by a long run of whitespace with no ``'`` after it,
    # and this filter runs synchronously on the event loop. Whitespace, ``=`` and ``'``
    # exclude one another, so backtracking can never produce a match here.
    (re.compile(r"(?i)(password\s*+=?\s*+)'[^']*'"), rf"\1'{_REDACTED}'"),
    (re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]+"), f"AGE-SECRET-KEY-{_REDACTED}"),
    # An API token (RC-322): ``zad_`` plus 43 url-safe characters. The 40 is below the 43
    # of ``secrets.token_urlsafe(32)``, so a truncated value falls in as well.
    (re.compile(r"zad_[A-Za-z0-9_-]{40,}"), f"zad_{_REDACTED}"),
)


def sanitize_for_log(value: str | None, max_length: int = 64) -> str:
    """Sanitize a value for safe inclusion in log messages.

    Prevents log injection attacks by:
    - Truncating to max_length
    - Removing newlines and carriage returns
    - Removing other control characters

    Args:
        value: The value to sanitize
        max_length: Maximum length of the sanitized value (default: 64)

    Returns:
        Sanitized string safe for logging
    """
    if not value:
        return ""
    # Truncate, remove newlines/carriage returns, and strip control characters
    sanitized = value[:max_length].replace("\n", "").replace("\r", "")
    # Remove other ASCII control characters (0x00-0x1F except tab, 0x7F)
    sanitized = "".join(c for c in sanitized if ord(c) >= 32 or c == "\t")
    return sanitized


def redact_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential-bearing values masked.

    Matching is case-insensitive on the header name. Non-sensitive headers pass
    through unchanged so the log still shows the request shape.
    """
    return {key: (_REDACTED if str(key).lower() in _SENSITIVE_HEADERS else value) for key, value in headers.items()}


def redact_text(text: str) -> str:
    """Mask the known secret shapes in a free text."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_structure(value: Any) -> Any:
    """Mask the secret shapes in every string inside a dict, a list or a text.

    For the ``result`` field of a task: that goes to the client as JSON and carries the same
    error text as ``error_message``.
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
    """Masks the known secret shapes in every message, traceback and stack reaching a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            # Message and args do not fit together; the formatter reports that error. The
            # traceback below it still has to be masked.
            message = ""

        redacted = redact_text(message)
        if redacted != message:
            # The args are already in the message, so they have to go: a second formatting
            # round would otherwise put the secret back.
            record.msg = redacted
            record.args = None

        if record.exc_info and not record.exc_text:
            # Formatted here: the formatter only does that after this filter, by which time a
            # secret in the exception message has already reached the log. Our handlers use a
            # plain Formatter, so the output stays the same.
            record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


def fingerprint_for_log(value: str) -> str:
    """A short, non-reversible fingerprint of a secret, for the log.

    Eight hex characters of the SHA-256: enough to tie two log records about the same secret
    together, too little to be the secret itself.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:8]


#: The path of the public invite routes. The key is the segment behind ``/invite/``; what
#: follows it (``/register``, ``/sso/callback``) stays as it is.
_INVITE_PATH = re.compile(r"(/invite/)([^/?\s]+)")


def redact_invite_key_in_path(path: str) -> str:
    """Replace the invite key in a URL path with its fingerprint.

    The key is a bearer secret and sits in the path, so every record that logs a path
    verbatim puts it in the log. The routes log it as a fingerprint themselves; this is the
    same conversion for every record that writes a whole path, from the uvicorn access log
    to the error records in the middleware.
    """
    return _INVITE_PATH.sub(lambda match: match.group(1) + fingerprint_for_log(match.group(2)), path)
