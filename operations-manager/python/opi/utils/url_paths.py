"""Tenant-supplied URL paths: one charset, one normalisation.

``logout-path`` becomes a URL that Keycloak itself fetches, so it needs the same charset
guard as a component's ``match``/``rewrite`` values; hence this shared home instead of a
second copy next to the first.
"""

from __future__ import annotations

import re
from typing import Any

# URL-path charset voor tenant-bestuurde component match/rewrite waarden;
# voorkomt nginx-snippet-injection in ingress.yaml.jinja.
SAFE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9/_.~-]*$")


def sanitize_path_value(value: Any, field: str) -> str:
    """
    Validate a tenant-supplied URL path used for ingress routing/rewriting.

    Raises:
        ValueError: If the value is not a string or contains characters
            outside the safe URL-path charset (defense against nginx
            configuration-snippet injection).
    """
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004
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
        raise ValueError(f"logout-path must be a string, got {type(value).__name__}")  # noqa: TRY004
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
