"""Tenant-supplied URL paths: one charset, one normalisation.

``logout-path`` becomes a URL that Keycloak itself fetches, so it needs the same charset
guard as a component's ``match``/``rewrite`` values; hence this shared home instead of a
second copy next to the first.
"""

from __future__ import annotations

import re
from typing import Any

# URL path charset for tenant-controlled component match/rewrite values; prevents
# nginx-snippet injection in ingress.yaml.jinja. ``\Z`` and not ``$``: in Python the latter
# also matches before a trailing newline, which let '/metrics\n' through.
SAFE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9/_.~-]*\Z")


#: The characters the URL parser removes before it parses, so the prefix check below has
#: to remove them first: a browser reads '/<TAB>/host' as '//host', another domain.
_REMOVED_BY_THE_URL_PARSER = str.maketrans({"\t": None, "\n": None, "\r": None})


def local_redirect_path(value: str, default: str = "/") -> str:
    """This value as a redirect target on this site, or ``default``.

    ``//host`` and ``/\\host`` are another domain to a browser, not a path.
    """
    cleaned = value.translate(_REMOVED_BY_THE_URL_PARSER)
    if cleaned.startswith("/") and not cleaned.startswith(("//", "/\\")):
        return cleaned
    return default


def sanitize_path_value(value: Any, field: str) -> str:
    """Validate a tenant-supplied URL path used for ingress routing and rewriting.

    Raises:
        ValueError: if the value is not a string or holds characters outside the safe
            URL-path charset (defense against nginx configuration-snippet injection).
    """
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004  # the caller handles one error type, so a type mismatch raises ValueError too
            f"Component path '{field}' must be a string, got {type(value).__name__}"
        )
    if not value:
        raise ValueError(f"Component path '{field}' must not be empty")
    if not SAFE_PATH_PATTERN.match(value):
        raise ValueError(
            f"Component path '{field}' contains illegal characters: {value!r}. "
            f"Only '/', letters, digits and '-._~' are allowed (no whitespace, "
            f"quotes or newlines)."
        )
    return value


def normalize_logout_path(value: Any) -> str | None:
    """Normalise an opt-in ``logout-path`` to exactly one leading slash, or None.

    A missing or doubled leading slash is nothing to hand a user an error over, so it
    is normalised away. A trailing slash is kept exactly as written: ``/api/v1.0/logout/``
    is a different endpoint than ``/api/v1.0/logout``.

    Raises:
        ValueError: On an absolute URL, a non-string value, or a path outside the
            safe URL-path charset.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"logout-path must be a string, got {type(value).__name__}")  # noqa: TRY004  # see sanitize_path_value
    stripped = value.strip()
    if not stripped:
        return None
    if "://" in stripped:
        raise ValueError(
            f"logout-path must be a path on the component itself, not a full URL: {value!r}. "
            f"Write only the path, for example '/api/v1.0/logout/'; the platform puts the "
            f"deployment's own hostname in front of it."
        )
    return sanitize_path_value("/" + stripped.lstrip("/"), "logout-path")
