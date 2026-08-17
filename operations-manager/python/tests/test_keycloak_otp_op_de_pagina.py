"""De gedeelde OTP van een realm staat als VELD op de pagina, en de seed nooit (RC-101).

Hier stond ``test_keycloak_otp_code_endpoint.py``. Dat endpoint
(``/projects/<p>/keycloak/<realm>/otp-code``) leverde de code op verzoek, achter een knop
"Toon code" in het Keycloak-blok. De OTP is nu een veld zoals het wachtwoord ernaast:
gemaskeerd, met een oogje en het klembord erin, met de code uit de paginarender. Daarmee
had het endpoint geen enkele aanroeper meer en is het samen met zijn fragment weg.

Wat WEL blijft gelden, en hier bewaakt wordt, is de eigenschap waarvoor dat endpoint ooit
gemaakt is: **de seed bereikt de pagina nooit**. De seed geeft voor altijd codes; de code
op de pagina vergaat binnen een periode van 30 seconden. Wie de code kan zien mag ook het
admin-wachtwoord zien - hetzelfde blok, dezelfde rolpoort - en dat is het langstlevende
van de twee.
"""

from __future__ import annotations

from pathlib import Path

from opi.core.templates_lotc import templates_lotc as templates
from opi.utils.totp import totp_now

SECTION = Path(__file__).parent.parent / "opi/services/catalog/keycloak/section-detail.html.j2"

RAW_SEED = "12345678901234567890"
#: Wat een authenticator zou inslikken als de seed toch zou renderen.
SEED_BASE32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _render(**realm_extra: object) -> str:
    realm = {
        "host": "https://kc.example",
        "realm": "demo-realm",
        "username": "demo_admin",
        "password": "VOORBEELDWAARDE-geen-echt-geheim",
        **realm_extra,
    }
    section = type("Section", (), {"context": {"realms": [realm]}})()
    return templates.env.get_template("keycloak/section-detail.html.j2").render(
        section=section, project={"name": "demo"}
    )


def test_de_code_staat_als_veld_op_de_pagina() -> None:
    code, _ = totp_now(RAW_SEED)

    html = _render(has_totp=True, totp_code=code)

    assert code in html, "de OTP-code staat niet op de pagina"
    assert "Gedeelde OTP" in html


def test_de_code_zit_in_hetzelfde_soort_veld_als_het_wachtwoord() -> None:
    """Gemaskeerd met een oogje en een klembord, niet als losse knop met een codeblok."""
    html = _render(has_totp=True, totp_code="123456")

    assert "Toon code" not in html, "de knop is vervangen door een veld"
    assert "otp-code" not in html, "het opvraag-endpoint bestaat niet meer"
    assert html.count('data-lotc-component="secret-field"') >= 2, (
        "wachtwoord en OTP horen allebei in zo'n veld te staan"
    )


def test_de_hulptekst_zegt_dat_de_code_veroudert() -> None:
    """Een code die stilletjes verlopen is, is erger dan een code die zegt dat hij oud is.
    Hij ververst niet vanzelf; de pagina opnieuw laden geeft een verse."""
    html = _render(has_totp=True, totp_code="123456")

    assert "30 seconden" in html
    assert "opnieuw" in html


def test_zonder_otp_staat_er_geen_veld() -> None:
    html = _render()

    assert "Gedeelde OTP" not in html


def test_de_seed_rendert_nooit() -> None:
    """De reden dat er ooit een endpoint voor was. Blijft gelden nu de code in de
    paginarender zit: het sjabloon kent alleen de code."""
    html = _render(has_totp=True, totp_code="123456", totp_secret=RAW_SEED)

    assert RAW_SEED not in html
    assert SEED_BASE32 not in html


def test_het_sjabloon_noemt_de_seed_niet_en_laadt_geen_cdn_script() -> None:
    """Drift guard op de bron. De seed werd hier ooit gerenderd, naast een script van
    jsdelivr dat hem kon lezen. Beide zijn weg; dit valt om als een van de twee terugkomt."""
    source = SECTION.read_text()

    assert "totp_secret" not in source
    assert "totp_otpauth_uri" not in source
    assert "cdn.jsdelivr.net" not in source
