"""Zet elke pagina van het Keycloak-thema op de foto, in twee talen en twee breedtes.

Waarom dit bestaat
------------------
Een groene rendertoets zegt dat de pagina uit ons thema komt, niet dat hij er goed
uitziet. De visuele kant van `features/keycloak-nldd-thema.md` beoordeelt een mens, en
dit script levert het beeld waarop dat gebeurt.

De pagina's worden niet opgevraagd maar DOORLOPEN: een FreeMarker-pagina van Keycloak
heeft geen eigen adres, je komt er door het voorgaande formulier te versturen. Playwright
vult de velden dus echt in en klikt echt op de knop, en daarmee is tegelijk gemeten dat de
NLDD-invoervelden hun waarde posten (ze leveren die via ElementInternals, niet met een
native input).

Gebruik
-------
    # In een andere terminal, en laat hem draaien:
    task test-keycloak-image

    cd operations-manager/python
    uv run python scripts/keycloak_thema_shots.py --url http://localhost:58081 \\
        --admin admin --wachtwoord '<het wachtwoord dat de taak toont>'

De beelden landen in ``tests/e2e/screenshots/keycloak-thema/`` als
``<pagina>-<taal>-<breedte>.png``, met een contactvel ernaast:

    tests/e2e/screenshots/keycloak-thema/index.html

Vier dingen die je moet weten voordat je een beeld gelooft
----------------------------------------------------------
1. NLDD is een webcomponentenlaag. Een beeld dat genomen wordt voordat de browser de
   custom elements heeft opgebouwd toont ongestileerde tekst. Vandaar ``_wacht``.
2. ``task test-keycloak-image`` verzint het adminwachtwoord per ronde en toont het in zijn
   uitvoer; er staat geen wachtwoordvorm in de repo.
3. Drie pagina's staan er niet op, en kunnen er niet op: de twee die een externe IdP
   vragen (`login-idp-link-confirm`, `login-idp-link-email`) en `select-authenticator`,
   die pas verschijnt bij meer dan een alternatief in de flow.
4. `login-username.ftl` en `login-password.ftl` ook niet: die horen bij een flow die de
   twee stappen splitst, en de proefrealm hier gebruikt de gewone browser flow. Dat ze in
   ons thema renderen meet `tests/integration/test_keycloak_image_thema.py` op een eigen
   realm; hun opmaak is die van de twee helften van `login`, die hier wel op de foto staat.
"""

from __future__ import annotations

import argparse
import html as htmllib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from opi.utils.totp import totp_now
from playwright.sync_api import sync_playwright

if TYPE_CHECKING:
    from playwright.sync_api import Page

PYTHON_ROOT = Path(__file__).resolve().parents[1]
UITVOER = PYTHON_ROOT / "tests" / "e2e" / "screenshots" / "keycloak-thema"

THEMA = "nl-design-system"
REALM = "schermafdruk-thema"
CLIENT = "schermafdruk-probe"
WACHTWOORD = "Proef-wachtwoord-1234"

TIMEOUT = 30.0

#: De twee breedtes: een gewoon bureaubladscherm en een telefoon.
BREEDTES = {"desktop": (1280, 900), "telefoon": (390, 844)}

TALEN = ("nl", "en")

#: De vaste gebruikers van de proefrealm: naam -> de verplichte acties die hun pagina
#: oproepen. De OTP-gebruikers staan er niet bij: die krijgt elke ronde zijn eigen.
GEBRUIKERS = {
    "gewoon": [],
    "wachtwoord": ["UPDATE_PASSWORD"],
    "profiel": ["UPDATE_PROFILE"],
    "mail": ["VERIFY_EMAIL"],
}


def _redirect(url: str) -> str:
    """De redirect-URI van de proefclient, op Keycloak zelf.

    Een URI naar een dode poort geeft na een voltooide inlog de foutpagina van de browser.
    Keycloak antwoordt op dit pad met een 404; er wordt niets van gefotografeerd, het is
    alleen een adres dat bestaat.
    """
    return f"{url}/realms/{REALM}/klaar"


def _otp_gebruiker(taal: str, breedte: str) -> str:
    """Een eigen OTP-gebruiker per ronde.

    Na het instellen heeft de gebruiker zijn credential, en dan toont de volgende inlog het
    INVOERscherm in plaats van de instelpagina. Met een gebruiker per ronde ziet elke ronde
    beide pagina's.
    """
    return f"otp-{taal}-{breedte}"


# ----------------------------------------------------------------------- de proefrealm


def _token(url: str, admin: str, wachtwoord: str) -> str:
    antwoord = httpx.post(
        f"{url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": admin,
            "password": wachtwoord,
        },
        timeout=TIMEOUT,
    )
    antwoord.raise_for_status()
    return antwoord.json()["access_token"]


def _maak_realm(url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    realms = f"{url}/admin/realms"
    httpx.delete(f"{realms}/{REALM}", headers=headers, timeout=TIMEOUT)
    gemaakt = httpx.post(
        realms,
        headers=headers,
        json={
            "realm": REALM,
            "enabled": True,
            "displayName": "Proefomgeving",
            "loginTheme": THEMA,
            "accountTheme": THEMA,
            "internationalizationEnabled": True,
            "supportedLocales": ["nl", "en"],
            "defaultLocale": "nl",
            "registrationAllowed": True,
            "resetPasswordAllowed": True,
            "rememberMe": True,
        },
        timeout=TIMEOUT,
    )
    gemaakt.raise_for_status()

    admin = f"{realms}/{REALM}"
    httpx.post(
        f"{admin}/clients",
        headers=headers,
        json={
            "clientId": CLIENT,
            "publicClient": True,
            "standardFlowEnabled": True,
            "redirectUris": [f"{_redirect(url)}*"],
            "attributes": {"pkce.code.challenge.method": ""},
        },
        timeout=TIMEOUT,
    ).raise_for_status()

    gebruikers = dict(GEBRUIKERS)
    for breedte in BREEDTES:
        for taal in TALEN:
            gebruikers[_otp_gebruiker(taal, breedte)] = ["CONFIGURE_TOTP"]

    for naam, acties in gebruikers.items():
        httpx.post(
            f"{admin}/users",
            headers=headers,
            json={
                "username": naam,
                "enabled": True,
                "email": f"{naam}@toets.invalid",
                "emailVerified": naam != "mail",
                "firstName": "Proef",
                "lastName": naam,
                "requiredActions": acties,
                "credentials": [{"type": "password", "value": WACHTWOORD, "temporary": False}],
            },
            timeout=TIMEOUT,
        ).raise_for_status()


def _ruim_realm_op(url: str, token: str) -> None:
    httpx.delete(
        f"{url}/admin/realms/{REALM}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )


# -------------------------------------------------------------------------- de beelden


def _wacht(page: Page) -> None:
    """Wacht tot de browser elk custom element heeft opgebouwd.

    ``:not(:defined)`` selecteert precies de tags die de browser nog niet kent. Zolang er
    een over is toont een beeld ongestileerde tekst en klopt geen enkele afmeting.
    """
    page.wait_for_load_state("networkidle")
    try:
        page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)
    except Exception:
        print("      ! niet elk component was opgebouwd; het beeld kan afwijken")
    # Gemeten: met 300 ms stond de taalkeuze soms nog niet in de balk. nldd-menu-bar meet
    # zijn eigen breedte na de eerste opbouw, en die slag valt buiten `:defined`.
    page.wait_for_timeout(1200)


def _schiet(page: Page, naam: str, taal: str, breedte: str) -> None:
    _wacht(page)
    pad = UITVOER / f"{naam}-{taal}-{breedte}.png"
    page.screenshot(path=str(pad), full_page=True)
    print(f"      {pad.name}")


def _auth_url(url: str, taal: str, client_id: str = CLIENT) -> str:
    return (
        f"{url}/realms/{REALM}/protocol/openid-connect/auth"
        f"?client_id={client_id}&response_type=code&scope=openid"
        f"&redirect_uri={_redirect(url)}&kc_locale={taal}"
    )


def _log_in(page: Page, url: str, taal: str, gebruiker: str) -> None:
    """Vult het inlogformulier echt in en verstuurt het."""
    page.goto(_auth_url(url, taal))
    _wacht(page)
    page.fill("#username input", gebruiker)
    page.fill("#password input", WACHTWOORD)
    page.click("#kc-login")
    page.wait_for_load_state("load")


def _ronde(page: Page, url: str, taal: str, breedte: str) -> None:
    print(f"   {taal} / {breedte}")

    page.goto(_auth_url(url, taal))
    _schiet(page, "login", taal, breedte)

    # De foutweg: een afgekeurd wachtwoord.
    page.fill("#username input", "gewoon")
    page.fill("#password input", "fout-wachtwoord")
    page.click("#kc-login")
    page.wait_for_load_state("load")
    _schiet(page, "login-fout", taal, breedte)

    page.goto(_auth_url(url, taal))
    _wacht(page)
    page.click("a[href*='registration']")
    _schiet(page, "register", taal, breedte)

    page.goto(_auth_url(url, taal))
    _wacht(page)
    page.click("a[href*='reset-credentials']")
    _schiet(page, "login-reset-password", taal, breedte)
    page.fill("#username input", "gewoon")
    page.click("#kc-submit")
    page.wait_for_load_state("load")
    _schiet(page, "info", taal, breedte)

    _log_in(page, url, taal, "wachtwoord")
    _schiet(page, "login-update-password", taal, breedte)

    _log_in(page, url, taal, "profiel")
    _schiet(page, "login-update-profile", taal, breedte)

    _log_in(page, url, taal, "mail")
    _schiet(page, "login-verify-email", taal, breedte)

    # De OTP-weg: eerst instellen, dan gebruiken. Deze ronde heeft zijn eigen gebruiker,
    # want na het instellen toont een volgende inlog het invoerscherm.
    otp_gebruiker = _otp_gebruiker(taal, breedte)
    _log_in(page, url, taal, otp_gebruiker)
    _schiet(page, "login-config-totp", taal, breedte)
    geheim = re.search(r'name="totpSecret" value="([^"]+)"', page.content())
    if geheim is None:
        print("      ! geen totpSecret op de pagina; login-otp komt er deze ronde niet op")
    else:
        page.fill("#totp input", totp_now(geheim.group(1))[0])
        page.fill("#userLabel input", "proef-app")
        page.click("#saveTOTPBtn")
        page.wait_for_load_state("load")

        _log_in(page, url, taal, otp_gebruiker)
        _schiet(page, "login-otp", taal, breedte)
        page.fill("#otp input", totp_now(geheim.group(1))[0])
        page.click("#kc-login")
        page.wait_for_load_state("load")
        if not page.url.startswith(_redirect(url)):
            print(f"      ! de OTP-inlog kwam niet bij {_redirect(url)} uit maar bij {page.url}")

    _log_in(page, url, taal, "gewoon")
    page.goto(f"{url}/realms/{REALM}/protocol/openid-connect/logout")
    _schiet(page, "logout-confirm", taal, breedte)

    page.goto(_auth_url(url, taal, client_id="bestaat-niet"))
    _schiet(page, "error", taal, breedte)

    page.goto(f"{url}/")
    _schiet(page, "welcome", taal, breedte)


def _contactvel() -> None:
    beelden = sorted(p.name for p in UITVOER.glob("*.png"))
    rijen = "\n".join(
        f'<figure><img src="{htmllib.escape(naam)}" alt="{htmllib.escape(naam)}">'
        f"<figcaption>{htmllib.escape(naam)}</figcaption></figure>"
        for naam in beelden
    )
    (UITVOER / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Keycloak-thema</title>"
        "<style>body{font:14px system-ui;margin:24px;background:#f4f4f4}"
        "figure{margin:0 0 32px;background:#fff;padding:12px;border-radius:8px}"
        "img{max-width:100%;display:block;border:1px solid #ddd}"
        "figcaption{padding-top:8px;color:#555}</style>"
        f"<h1>Keycloak-thema, {len(beelden)} beelden</h1>{rijen}\n"
    )
    print(f"\nContactvel: {UITVOER / 'index.html'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:58081", help="de Keycloak van task test-keycloak-image")
    parser.add_argument("--admin", default="admin")
    parser.add_argument("--wachtwoord", required=True, help="het adminwachtwoord dat de taak toont")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    UITVOER.mkdir(parents=True, exist_ok=True)
    for oud in UITVOER.glob("*.png"):
        oud.unlink()

    token = _token(url, args.admin, args.wachtwoord)
    _maak_realm(url, token)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for breedte, (w, h) in BREEDTES.items():
                    for taal in TALEN:
                        context = browser.new_context(viewport={"width": w, "height": h})
                        try:
                            _ronde(context.new_page(), url, taal, breedte)
                        finally:
                            context.close()
            finally:
                browser.close()
    finally:
        _ruim_realm_op(url, token)

    _contactvel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
