"""Redirect-URI's van een project: één regel, één afleiding.

Dezelfde waarden worden gelezen door het formulier, het configmodel en de
Keycloak-connector. De regel en de origin-afleiding staan daarom hier, net als bij
``url_paths``, in plaats van een keer per lezer.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: Loopback-hosts waarvoor http:// nog mag: een ontwikkelmachine heeft geen certificaat.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: De regel in één zin, voor een gebruiker die hem overtrad.
REDIRECT_URI_MESSAGE = (
    "Geef een volledige redirect URI met een concrete host, bijvoorbeeld "
    "https://app.rijksapps.nl/oauth2/callback of https://app.rijksapps.nl/*. "
    "http:// mag alleen naar localhost of 127.0.0.1, een wildcard mag niet in de hostnaam "
    "staan, en * alleen is geen geldige waarde."
)


def validate_redirect_uri(value: Any, field: str) -> str:
    """
    Toets een redirect URI die een project zelf opgeeft.

    Toegestaan is ``https://<host>[:poort]/<pad>`` met een concrete host, en
    ``http://`` alleen naar een loopback-adres. Een wildcard mag in het pad staan,
    niet in de host.

    Raises:
        ValueError: Op een andere vorm, een ander schema, een wildcard-host, een
            fragment, of whitespace en control characters in de waarde.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")  # noqa: TRY004
    if not value:
        raise ValueError(f"{field} must not be empty")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} must not contain whitespace or control characters: {value!r}")
    if "#" in value:
        raise ValueError(f"{field} must not contain a fragment: {value!r}")

    parts = urlsplit(value)
    # De hele netloc, niet alleen de hostname: ``derive_web_origin`` neemt de netloc over,
    # dus een ``*`` in de poort of de userinfo levert net zo goed een onzin-origin op.
    if "*" in parts.netloc:
        raise ValueError(f"{field} must not use a wildcard in the host: {value!r}")
    # Userinfo gaat mee in de origin, en daar komt hij met geen enkele browser-origin
    # overeen. Bovendien leest urlsplit de host na de ``@`` waar een browser de naam
    # ervoor kan nemen, dus een hostpoort die hier op ``hostname`` toetst wordt omzeild.
    if "@" in parts.netloc:
        raise ValueError(f"{field} must not contain userinfo before the host: {value!r}")
    if parts.scheme == "https":
        # De HOST voor deze toets, niet de netloc: bij ``https://:8443/cb`` is de netloc
        # gevuld en de host leeg, en dan zou de origin ``https://:8443`` worden.
        if not parts.hostname:
            raise ValueError(f"{field} must name a host: {value!r}")
    elif parts.scheme == "http":
        if parts.hostname not in LOOPBACK_HOSTS:
            raise ValueError(f"{field} may only use http:// for localhost or 127.0.0.1: {value!r}")
    else:
        raise ValueError(f"{field} must start with https:// (or http:// for a loopback address): {value!r}")
    return value


def validate_redirect_uris(values: Any, field: str) -> list[str]:
    """Toets een lijst redirect URI's. Een lege lijst blijft leeg, dat is geen wildcard."""
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list, got {type(values).__name__}")  # noqa: TRY004
    return [validate_redirect_uri(value, f"{field}[{index}]") for index, value in enumerate(values)]


def derive_web_origin(uri: str) -> str:
    """De web origin van een redirect URI: schema plus host en poort, zonder pad."""
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"
