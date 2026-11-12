"""Redirect URIs of a project: one rule, one derivation.

The same values are read by the form, the config model and the Keycloak connector. The rule
and the origin derivation therefore live here, as with ``url_paths``, instead of once per
reader.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: Loopback hosts that may still use http://: a development machine has no certificate.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: The rule in one sentence, for a user who broke it.
REDIRECT_URI_MESSAGE = (
    "Geef een volledige redirect URI met een concrete host, bijvoorbeeld "
    "https://app.rijksapps.nl/oauth2/callback of https://app.rijksapps.nl/*. "
    "http:// mag alleen naar localhost of 127.0.0.1, een wildcard mag niet in de hostnaam "
    "staan, en * alleen is geen geldige waarde."
)


def validate_redirect_uri(value: Any, field: str) -> str:  # noqa: ANN401  # a project-file value
    """Validate a redirect URI a project supplies itself.

    Allowed is ``https://<host>[:port]/<path>`` with a concrete host, and ``http://`` only
    towards a loopback address. A wildcard may sit in the path, not in the host.

    Raises:
        ValueError: on another shape, another scheme, a wildcard host, a fragment, or
            whitespace and control characters in the value.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")  # noqa: TRY004  # the caller handles one error type, so a type mismatch raises ValueError too
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} must not contain whitespace or control characters: {value!r}")
    if "#" in value:
        raise ValueError(f"{field} must not contain a fragment: {value!r}")

    parts = urlsplit(value)
    # The whole netloc, not just the hostname: ``derive_web_origin`` carries the netloc over,
    # so a ``*`` in the port or the userinfo yields a nonsense origin just as well.
    if "*" in parts.netloc:
        raise ValueError(f"{field} must not use a wildcard in the host: {value!r}")
    # Userinfo travels into the origin, where it matches no browser origin at all. On top of
    # that, urlsplit reads the host after the ``@`` where a browser may take the name before
    # it, so a host guard checking ``hostname`` here is bypassed.
    if "@" in parts.netloc:
        raise ValueError(f"{field} must not contain userinfo before the host: {value!r}")
    if parts.scheme == "https":
        # The HOST for this check, not the netloc: with ``https://:8443/cb`` the netloc is
        # filled and the host empty, and the origin would become ``https://:8443``.
        if not parts.hostname:
            raise ValueError(f"{field} must name a host: {value!r}")
    elif parts.scheme == "http":
        if parts.hostname not in LOOPBACK_HOSTS:
            raise ValueError(f"{field} may only use http:// for localhost or 127.0.0.1: {value!r}")
    else:
        raise ValueError(f"{field} must start with https:// (or http:// for a loopback address): {value!r}")
    return value


def validate_redirect_uris(values: Any, field: str) -> list[str]:  # noqa: ANN401  # a project-file value
    """Validate a list of redirect URIs. An empty list stays empty, that is not a wildcard."""
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list, got {type(values).__name__}")  # noqa: TRY004  # the caller handles one error type, so a type mismatch raises ValueError too
    return [validate_redirect_uri(value, f"{field}[{index}]") for index, value in enumerate(values)]


def derive_web_origin(uri: str) -> str:
    """The web origin of a redirect URI: scheme plus host and port, without the path."""
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"
