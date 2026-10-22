"""De inline SVG van een QR-code (RC-329): geen XML-declaratie, een scanbare achtergrond."""

from __future__ import annotations

from opi.utils.qr import qr_svg


def test_qr_svg_is_inline_markup_zonder_xml_declaratie() -> None:
    svg = qr_svg("otpauth://totp/test:a@b.nl?secret=ABC")
    assert svg.startswith("<svg")
    assert "<?xml" not in svg


def test_qr_svg_heeft_een_witte_achtergrond_voor_de_donkere_weergave() -> None:
    svg = qr_svg("otpauth://totp/test:a@b.nl?secret=ABC")
    assert 'fill="#fff"' in svg
    assert 'stroke="#000"' in svg


def test_qr_svg_verschilt_per_payload() -> None:
    assert qr_svg("eerste-payload") != qr_svg("tweede-payload")
