"""Log in op de sandbox en maak een schermafbeelding van een pagina.

Waarom dit bestaat: bijna elke ronde vormgeving strandde erop dat een groene test niet
laat zien hoe iets ERUIT ziet. Dit logt in en levert een plaatje, zodat er naar het scherm
gekeken kan worden in plaats van naar markup.

    uv run python scripts/kijk_sandbox.py /projects/deployments/test-uy9
    uv run python scripts/kijk_sandbox.py /projects --breedte 1920 --uit /tmp/x.png
    uv run python scripts/kijk_sandbox.py /projects --deel "#deployments-lijst"

De sessie wordt bewaard in ``.sandbox-sessie.json`` zodat een volgende aanroep niet opnieuw
hoeft in te loggen; verwijder dat bestand als het inloggen misgaat.
"""

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASIS = os.environ.get("ZAD_SANDBOX_URL", "https://zad.sandbox.rijksapp.dev")
GEBRUIKER = os.environ.get("ZAD_SANDBOX_USER", "admin")
WACHTWOORD = os.environ.get("ZAD_SANDBOX_PASSWORD", "admin1234")
SESSIE = Path(__file__).parent / ".sandbox-sessie.json"


def _inloggen(page: Page) -> None:
    """Doorloop het Keycloak-formulier. Doet niets als we al binnen zijn."""
    page.goto(f"{BASIS}/projects", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if "/auth/" not in page.url and "openid-connect" not in page.url:
        return
    # Keycloak zet zijn velden neer als #username / #password; de knop heet niet overal
    # hetzelfde, dus op type submit klikken is stabieler dan op een label.
    page.fill("#username", GEBRUIKER)
    page.fill("#password", WACHTWOORD)
    page.click("input[type=submit], button[type=submit]")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pad", help="pad op de sandbox, bijvoorbeeld /projects/details/test-uy9")
    p.add_argument("--uit", default="/tmp/zad-sandbox.png", help="waar het plaatje heen gaat")
    p.add_argument("--breedte", type=int, default=1280)
    p.add_argument("--hoogte", type=int, default=1400)
    p.add_argument("--deel", default=None, help="CSS-selector: alleen dat element in beeld")
    p.add_argument("--wacht", type=int, default=3000, help="ms wachten na het laden")
    a = p.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": a.breedte, "height": a.hoogte},
            storage_state=str(SESSIE) if SESSIE.exists() else None,
            ignore_https_errors=True,
        )
        page = context.new_page()
        fouten: list[str] = []
        page.on("console", lambda m: fouten.append(m.text) if m.type == "error" else None)

        _inloggen(page)
        context.storage_state(path=str(SESSIE))

        page.goto(f"{BASIS}{a.pad}", wait_until="domcontentloaded")
        page.wait_for_timeout(a.wacht)

        if "/auth/" in page.url or "openid-connect" in page.url:
            print("NIET INGELOGD - verwijder", SESSIE, "en probeer opnieuw", file=sys.stderr)
            return 1

        if a.deel:
            page.locator(a.deel).first.screenshot(path=a.uit)
        else:
            page.screenshot(path=a.uit, full_page=True)

        print(f"URL:    {page.url}")
        print(f"TITEL:  {page.title()}")
        print(f"BEELD:  {a.uit}")
        if fouten:
            print("CONSOLEFOUTEN:")
            for f in fouten[:8]:
                print("  -", f[:160])
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
