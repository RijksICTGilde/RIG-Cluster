"""A QR code as inline SVG.

Server side and inline, because the page that needs a QR carries a secret and a script from a
CDN does not belong there. See ``features/keycloak-realm-admin-otp.md``.
"""

from __future__ import annotations

import io

import segno


def qr_svg(payload: str, scale: int = 5) -> str:
    """SVG markup without an XML declaration, so it can be embedded in a page."""
    buffer = io.BytesIO()
    # An explicit white background: in dark mode the black modules would otherwise sit on a
    # dark card and there is nothing left to scan.
    segno.make(payload, error="m").save(
        buffer,
        kind="svg",
        scale=scale,
        border=2,
        dark="#000000",
        light="#ffffff",
        xmldecl=False,
        omitsize=True,
    )
    return buffer.getvalue().decode()
