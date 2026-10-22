"""Een QR-code als inline SVG.

Server-side en inline, want de pagina die een QR nodig heeft draagt een geheim: een
script van een CDN hoort daar niet bij. Zie de aanwijzing in
``features/keycloak-realm-admin-otp.md``.
"""

from __future__ import annotations

import io

import segno


def qr_svg(payload: str, scale: int = 5) -> str:
    """De QR van ``payload`` als SVG-markup, zonder XML-declaratie."""
    buffer = io.BytesIO()
    # Eigen witte achtergrond: in de donkere weergave staan zwarte modules anders op een
    # donkere kaart en is er niets te scannen.
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
