"""Taak 5 van RC-108: de handmatige doorloop over de sandbox, met schermafbeeldingen.

Waarom een script en geen test: dit is geen poort maar een MEETBANK. Het maakt via de
echte wizard een project met een component, publish-on-web, een database en Keycloak,
rolt het uit, en legt daarna elk tabblad van de projectpagina vast zodat er naar het
scherm gekeken kan worden in plaats van naar markup. De schermafbeeldingen zijn de
opbrengst; het script beweert zelf niets over vormgeving.

    uv run python scripts/doorloop_rc108.py --uit /workspace/docs/doorloop-rc108

Het project blijft staan als er iets misgaat, zodat de toestand te bekijken is; met
--opruimen wordt het aan het eind via de API verwijderd.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright
from tests.e2e.helpers import cluster
from tests.e2e.helpers.forgejo import ForgejoClient
from tests.e2e.helpers.lifecycle import (
    RUNNABLE_IMAGE,
    project_name_from_progress,
    walk_create_wizard_with_services,
)
from tests.e2e.helpers.wizard import unique_project_name

BASIS = os.environ.get("ZAD_SANDBOX_URL", "https://zad.sandbox.rijksapp.dev")
GEBRUIKER = os.environ.get("ZAD_SANDBOX_USER", "admin")
WACHTWOORD = os.environ.get("ZAD_SANDBOX_PASSWORD", "admin1234")
FORGEJO = os.environ.get("FORGEJO_URL", "https://forgejo.sandbox.rijksapp.dev")
FORGEJO_USER = os.environ.get("FORGEJO_USER", "rig-admin")
FORGEJO_PW = os.environ.get("FORGEJO_PASSWORD", "admin1234")

#: De sandbox draagt een echt Let's Encrypt-wildcardcertificaat, dus de controle staat AAN:
#: een doorloop die de certificaatfout wegzet kan hem ook niet meer melden, en juist "geen
#: certificaatfout" is een van de dingen die deze taak moet aantonen. Uitschakelbaar via
#: dezelfde variabele als de testhelpers, voor een cluster met een zelfondertekend certificaat.
VERIFIEER = os.environ.get("FORGEJO_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

#: De tabbladen van de projectpagina, in de volgorde van de tabbalk. "services-info"
#: verschijnt alleen als een dienst er een blok voor levert (TABS_MET_VOORWAARDE).
TABBLADEN = [
    ("details", "01-overzicht"),
    ("team", "02-team"),
    ("componenten", "03-componenten"),
    ("services", "04-services"),
    ("services-info", "05-services-info"),
    ("deployments", "06-deployments"),
    ("metrics", "07-metrics"),
    ("backups", "08-backups"),
    ("taken", "09-taken"),
]


def inloggen(page: Page) -> None:
    """Doorloop het Keycloak-formulier; doet niets als we al binnen zijn."""
    page.goto(f"{BASIS}/projects", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if "/auth/" not in page.url and "openid-connect" not in page.url:
        return
    page.fill("#username", GEBRUIKER)
    page.fill("#password", WACHTWOORD)
    page.click("input[type=submit], button[type=submit]")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)


def projectbestand(naam: str) -> str:
    """De RUWE inhoud van het projectbestand, zoals het in git staat."""
    r = httpx.get(
        f"{FORGEJO}/api/v1/repos/{FORGEJO_USER}/zad-projects/raw/projects/{naam}.yaml",
        auth=(FORGEJO_USER, FORGEJO_PW),
        verify=VERIFIEER,
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def versie_controleren(verwacht: str) -> None:
    """Draait er nog steeds wat we denken? Voor ELKE schermmeting opnieuw (RC-108)."""
    r = httpx.get(f"{BASIS}/version", verify=VERIFIEER, timeout=30)
    draait = r.json().get("version", "")
    if not draait.startswith(verwacht):
        raise SystemExit(f"De sandbox draait {draait!r} en niet {verwacht!r} - elke schermmeting is dan waardeloos")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uit", default="/workspace/docs/doorloop-rc108", help="waar de plaatjes heen gaan")
    p.add_argument("--commit", required=True, help="de commit die op de sandbox hoort te draaien")
    p.add_argument("--project", default="", help="een BESTAAND project meten in plaats van er een maken")
    p.add_argument("--opruimen", action="store_true", help="het project aan het eind verwijderen")
    a = p.parse_args()

    uit = Path(a.uit)
    uit.mkdir(parents=True, exist_ok=True)
    versie_controleren(a.commit)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1600}, ignore_https_errors=not VERIFIEER)
        page = context.new_page()
        inloggen(page)

        forgejo = ForgejoClient(FORGEJO, FORGEJO_USER, FORGEJO_PW, "zad-projects", verify_ssl=VERIFIEER)

        if a.project:
            naam = a.project
            print(f"[doorloop] bestaand project: {naam}")
        else:
            weergavenaam = unique_project_name()
            print(f"[doorloop] wizard: {weergavenaam}")
            walk_create_wizard_with_services(
                page,
                BASIS,
                weergavenaam,
                user_email=f"{GEBRUIKER}@sandbox.rijksapp.dev",
                # postgresql-database en niet de namespace-variant: die is hidden=True en heeft
                # dus bewust geen wizardkaart (hij gaat via de API).
                services=["publish-on-web", "postgresql-database", "keycloak"],
                component_name="web",
                image=RUNNABLE_IMAGE,
            )
            naam = project_name_from_progress(page)
            print(f"[doorloop] aangemaakt: {naam}")

        print("[doorloop] wachten tot ArgoCD Healthy meldt")
        cluster.wait_for_project_apps_healthy(naam, timeout=300)
        print("[doorloop] Healthy")

        for tab, bestand in TABBLADEN:
            versie_controleren(a.commit)
            page.goto(f"{BASIS}/projects/{naam}/{tab}", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            doel = uit / f"{bestand}.png"
            page.screenshot(path=str(doel), full_page=True)
            titel = page.title()
            print(f"[tab] {tab:15} -> {doel.name}  ({titel}) url={page.url}")

        # Het projectbestand zoals het in git staat: waar RC-106 over gaat.
        (uit / "projectbestand.yaml").write_text(projectbestand(naam))
        print(f"[doorloop] projectbestand weggeschreven ({naam}.yaml)")

        print(f"PROJECT={naam}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
