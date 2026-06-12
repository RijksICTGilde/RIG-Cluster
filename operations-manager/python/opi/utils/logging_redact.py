"""Helpers for keeping secrets out of logs.

Logging request/response headers is useful for debugging, but headers routinely
carry credentials (``Authorization: Bearer …``, ``X-API-Key``, cookies). Logging
them verbatim leaks live tokens into the log stream. Always run header dicts
through :func:`redact_sensitive_headers` before logging them.
"""

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
