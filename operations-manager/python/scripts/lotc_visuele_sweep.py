"""Loop elke pagina en elke dialoog langs, maak er een schermafbeelding van, en meet.

Het gedragsoppervlak (``lotc_compare_behaviour.py``) zegt of een pagina hetzelfde DOET.
Het zegt niets over hoe hij eruitziet, en juist daar zit deze omzetting vol met stille
fouten: een icoon dat leeg rendert, een blok dat uit de andere componentomgeving komt en
door geen enkel stijlblad opgemaakt wordt, kaarten die elkaar exact raken omdat een ``gap``
niet oplost. Geen daarvan maakt een test rood. Alle drie zijn ze op een plaatje meteen te
zien.

Daarom dit script: het bezoekt elke route, opent elke dialoog, maakt van elk scherm een
schermafbeelding en toetst per scherm een handvol dingen die je op een plaatje zou zien
maar makkelijk over het hoofd ziet:

    - **roos-HTML op een NLDD-pagina.** ``data-roos-component`` of een ``rvo-``-klasse in
      het ANTWOORD. Dat is de meting die de bron niet kan geven: zulke HTML komt uit een
      tweede renderomgeving en is in de sjablonen onvindbaar.
    - **Onvervangen componenttags.** Een letterlijke ``<c-`` in de uitvoer betekent dat het
      sjabloon in de verkeerde omgeving rendert.
    - **Lege iconen.** Een NLDD-iconnaam die niet bestaat rendert leeg, zonder foutmelding.
    - **Fouten in de console en mislukte verzoeken.** Een 404 op een stylesheet of een script
      laat de pagina staan en de opmaak vallen.
    - **Kaarten die elkaar raken.** De BEREKENDE ``gap``, niet wat het sjabloon zegt: een
      CSS-variabele die niet oplost geeft ``0px`` en dat is precies zo een keer misgegaan.

Gebruik:

    uv run python scripts/lotc_visuele_sweep.py \\
        --base https://zad.sandbox.rijksapp.dev \\
        --secret <SECRET_KEY van de draaiende app> \\
        --email <adres op de allowlist> \\
        --uit /tmp/sweep

Elke bevinding komt met de naam van het scherm en het pad naar zijn schermafbeelding, zodat
je er meteen naar kunt KIJKEN - een bevinding zonder plaatje is precies wat deze ronde moest
voorkomen.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from itsdangerous import TimestampSigner
from playwright.sync_api import ConsoleMessage, Page, Request, sync_playwright

#: De pagina's. Elk pad, met een naam die in de bestandsnaam van de schermafbeelding komt.
PAGINAS: list[tuple[str, str]] = [
    ("dashboard", "/dashboard"),
    ("projecten", "/projects"),
    ("diensten", "/services"),
    ("beheer-gebruikers", "/admin/users"),
    ("beheer-goedkeuringen", "/admin/approvals"),
    ("beheer-gebruik", "/admin/usage"),
    # Hier stond /admin/domains, uit de docstring van build_lotc_admin die vier
    # beheerpagina's noemt. Die route bestaat niet: de zijkolom kent er drie
    # (navigation_lotc.py). Een verzonnen pad levert een 404 die als bevinding LIJKT.
    ("metrics-explorer", "/metrics-explorer"),
    ("over", "/about"),
    ("account", "/account"),
    ("wizard-start", "/forms/wizard/start"),
]

#: De tabbladen van de projectpagina. Elk is een eigen URL en dus een eigen scherm.
PROJECT_TABS = ("project", "componenten", "services", "deployments", "metrics", "taken")

#: De dialogen, als de JavaScript-aanroep die de PAGINA doet om ze te openen.
#:
#: Ze worden IN een pagina gezet en zijn daardoor precies het soort scherm dat je bij een
#: sweep overslaat: de pagina ziet er goed uit, en pas als je het venster opent staat er
#: iets uit de oude vormgeving in.
#:
#: Hier stond de fragment-URL, en die rechtstreeks bezoeken meet het verkeerde ding. Zo'n
#: fragment is een stuk HTML zonder ``<head>``: geen stijlbladen, geen design-system-CSS,
#: geen web-componenten die zich registreren. Elke berekende ``gap`` is dan nul en elk
#: icoon nul breed, en dat leverde ZEVEN bevindingen op die geen van alle bestonden - een
#: sweep die vals alarm geeft is erger dan geen sweep, want dan ga je hem wegklikken.
#:
#: Dus openen zoals de gebruiker het doet: de projectpagina laden en dezelfde functie
#: aanroepen die de knop aanroept.
DIALOGEN: list[tuple[str, str]] = [
    ("dialoog-identiteit", "openEditModal('modal-edit-identity', 'Projectgegevens')"),
    ("dialoog-team", "openEditModal('modal-edit-team', 'Team')"),
    ("dialoog-diensten", "openEditModal('modal-edit-services', 'Services')"),
    (
        "dialoog-herverwerken",
        "openServiceModal('/projects/{project}/actions/refresh-project/confirm', 'Project herverwerken')",
    ),
    (
        "dialoog-verwijderen",
        "openServiceModal('/projects/{project}/actions/delete-project/confirm', 'Project verwijderen')",
    ),
]

#: Wat de roos-omgeving in elke component achterlaat.
ROOS_MARKER = "data-roos-component"

#: Meldingen in de console die niets over deze omzetting zeggen. Een lijst die te ruim is
#: maakt de meting waardeloos, dus staat de reden erbij.
CONSOLE_NEGEER = (
    # Chart.js en htmx komen van een CDN; in een afgesloten omgeving is dat geen bevinding
    # over de vormgeving.
    "unpkg.com",
    "cdn.jsdelivr.net",
    # Favicon ontbreekt in de sandbox.
    "favicon",
)


@dataclass
class Scherm:
    """Een bezocht scherm, met wat eraan mankeerde."""

    naam: str
    url: str
    plaatje: Path | None = None
    bevindingen: list[str] = field(default_factory=list)


def sessie_cookie(secret: str, email: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": {"sub": "sweeper", "email": email, "name": "Sweeper"}}).encode()
    ).decode()
    return TimestampSigner(secret).sign(payload).decode()


#: De kop van de pagina die je krijgt als je WEL ingelogd bent maar niet op de allowlist
#: staat. Zonder deze controle is de sweep waardeloos op precies de manier die hij moet
#: voorkomen: elke route levert dan dezelfde nette toegangspagina, die pagina is gaaf, en
#: het script meldt opgewekt "0 bevindingen" over elf schermen die niemand heeft gezien.
#: Dat is een keer echt gebeurd, met admin@rijksoverheid.nl.
GEEN_TOEGANG = "Geen Toegang tot ZAD"


def meet_scherm(page: Page, scherm: Scherm, console: list[str], mislukt: list[str]) -> None:
    """Toets dit scherm op wat je op een plaatje zou zien maar makkelijk mist."""
    html = page.content()

    if GEEN_TOEGANG in html:
        scherm.bevindingen.append(
            "TOEGANGSPAGINA in plaats van het scherm: --email staat niet op de allowlist. "
            "Alles wat deze sweep verder meldt gaat over die pagina en niet over dit scherm."
        )
        return

    if ROOS_MARKER in html:
        scherm.bevindingen.append(f"roos-HTML in het antwoord ({ROOS_MARKER})")
    if "rvo-" in html:
        scherm.bevindingen.append("rvo-klassen in het antwoord: dit komt uit de andere componentomgeving")
    if "<c-" in html:
        scherm.bevindingen.append("onvervangen componenttag (<c-): dit sjabloon rendert in de verkeerde omgeving")

    # Een NLDD-icoon dat zijn naam niet kent rendert een leeg element. Gemeten op de
    # BEREKENDE grootte, want in de markup ziet een leeg icoon er hetzelfde uit als een vol.
    #
    # Alleen ZICHTBARE iconen tellen mee. Een element in een dichtgeklapt paneel of een
    # verborgen tabblad is ook nul breed, en zonder die uitzondering meldde deze meting
    # check-mark-circle en chart-x-y-axis-line als leeg terwijl NLDD ze allebei gewoon
    # levert - twee valse bevindingen, en die zijn duurder dan geen meting: je gaat de
    # echte ertussen missen.
    leeg = page.evaluate(
        """() => Array.from(document.querySelectorAll('nldd-icon, [data-lotc-component="icon"]'))
               .filter(el => el.offsetParent !== null && el.checkVisibility && el.checkVisibility())
               .filter(el => el.getBoundingClientRect().width === 0)
               .map(el => el.getAttribute('icon') || el.getAttribute('name') || '?')"""
    )
    if leeg:
        scherm.bevindingen.append(f"iconen die leeg renderen: {sorted(set(leeg))}")

    # De tussenruimte van een kaartenblok, berekend. Een gap die als CSS-variabele gezet
    # wordt zegt in het sjabloon niets: hij lost op of hij lost niet op, en in dat laatste
    # geval raken de kaarten elkaar exact. Zo is dat een keer ontdekt.
    plakkers = page.evaluate(
        """() => Array.from(document.querySelectorAll('.lotc-stack, .lotc-cluster, .lotc-grid'))
               .filter(el => el.children.length > 1)
               .filter(el => {
                   const g = getComputedStyle(el).gap;
                   return !g || g === 'normal' || parseFloat(g) === 0;
               })
               .map(el => el.className)
               .slice(0, 5)"""
    )
    if plakkers:
        scherm.bevindingen.append(f"blokken zonder tussenruimte (berekende gap 0): {sorted(set(plakkers))}")

    for regel in console:
        scherm.bevindingen.append(f"consolefout: {regel}")
    for regel in mislukt:
        scherm.bevindingen.append(f"verzoek mislukt: {regel}")


def bezoek(page: Page, naam: str, url: str, uit: Path) -> Scherm:
    scherm = Scherm(naam=naam, url=url)
    console: list[str] = []
    mislukt: list[str] = []

    def op_console(bericht: ConsoleMessage) -> None:
        if bericht.type == "error" and not any(n in bericht.text for n in CONSOLE_NEGEER):
            console.append(bericht.text[:200])

    def op_mislukt(verzoek: Request) -> None:
        if not any(n in verzoek.url for n in CONSOLE_NEGEER):
            mislukt.append(f"{verzoek.url} ({verzoek.failure})")

    page.on("console", op_console)
    page.on("requestfailed", op_mislukt)
    try:
        antwoord = page.goto(url, wait_until="networkidle", timeout=45000)
        if antwoord is not None and antwoord.status >= 400:
            scherm.bevindingen.append(f"HTTP {antwoord.status}")
        scherm.plaatje = uit / f"{naam}.png"
        page.screenshot(path=str(scherm.plaatje), full_page=True)
        meet_scherm(page, scherm, console, mislukt)
    except Exception as fout:
        scherm.bevindingen.append(f"kon niet laden: {type(fout).__name__}: {fout}")
    finally:
        page.remove_listener("console", op_console)
        page.remove_listener("requestfailed", op_mislukt)
    return scherm


def open_dialoog(page: Page, naam: str, basis: str, project: str, aanroep: str, uit: Path) -> Scherm:
    """Open een dialoog IN de projectpagina en meet hem daar.

    Zoals een gebruiker het doet: de pagina laden en dezelfde functie aanroepen die achter
    de knop hangt. Alleen zo staan de stijlbladen en de web-componenten er, en zegt een
    berekende ``gap`` of een lege icoonbreedte iets.
    """
    scherm = Scherm(naam=naam, url=f"{basis}/projects/details/{project} -> {aanroep}")
    console: list[str] = []
    mislukt: list[str] = []

    def op_console(bericht: ConsoleMessage) -> None:
        if bericht.type == "error" and not any(n in bericht.text for n in CONSOLE_NEGEER):
            console.append(bericht.text[:200])

    def op_mislukt(verzoek: Request) -> None:
        if not any(n in verzoek.url for n in CONSOLE_NEGEER):
            mislukt.append(f"{verzoek.url} ({verzoek.failure})")

    try:
        page.goto(f"{basis}/projects/details/{project}", wait_until="networkidle", timeout=45000)
        page.on("console", op_console)
        page.on("requestfailed", op_mislukt)

        ontbreekt = page.evaluate(
            "naam => typeof window[naam] !== 'function'",
            aanroep.split("(")[0],
        )
        if ontbreekt:
            scherm.bevindingen.append(f"de pagina kent {aanroep.split('(')[0]}() niet: de knop kan niets openen")
            return scherm

        page.evaluate(aanroep.format(project=project))
        # De inhoud komt met htmx binnen; wachten op stilte in plaats van op een vaste tijd.
        page.wait_for_timeout(2500)
        scherm.plaatje = uit / f"{naam}.png"
        page.screenshot(path=str(scherm.plaatje), full_page=True)
        meet_scherm(page, scherm, console, mislukt)
    except Exception as fout:
        scherm.bevindingen.append(f"kon niet openen: {type(fout).__name__}: {fout}")
    finally:
        page.remove_listener("console", op_console)
        page.remove_listener("requestfailed", op_mislukt)
    return scherm


def eerste_project(page: Page, basis: str) -> str | None:
    """De naam van een project op deze omgeving, uit de projectenlijst zelf.

    Uit de PAGINA en niet uit een vlag: een hardgecodeerde naam maakt het script stil
    onbruikbaar op een omgeving waar dat project niet bestaat, en dan meet je de lege lijst.
    """
    page.goto(f"{basis}/projects", wait_until="networkidle", timeout=45000)
    namen = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/projects/details/"]'))
               .map(a => a.getAttribute('href').split('/projects/details/')[1])
               .filter(Boolean)
               .map(s => s.split(/[?#/]/)[0])"""
    )
    return namen[0] if namen else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True)
    p.add_argument("--secret", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--uit", default="/tmp/lotc-sweep")
    p.add_argument("--project", default=None)
    p.add_argument("--breedte", type=int, default=1440)
    p.add_argument("--hoogte", type=int, default=900)
    args = p.parse_args()

    uit = Path(args.uit)
    uit.mkdir(parents=True, exist_ok=True)

    schermen: list[Scherm] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": args.breedte, "height": args.hoogte},
            ignore_https_errors=True,
        )
        context.add_cookies(
            [
                {
                    "name": "session",
                    "value": sessie_cookie(args.secret, args.email),
                    "url": args.base,
                }
            ]
        )
        page = context.new_page()

        project = args.project or eerste_project(page, args.base)
        if project is None:
            print("geen project gevonden op deze omgeving; de projectschermen worden overgeslagen")

        for naam, pad in PAGINAS:
            schermen.append(bezoek(page, naam, f"{args.base}{pad}", uit))

        if project:
            schermen.extend(
                bezoek(page, f"project-{tab}", f"{args.base}/projects/details/{project}?tab={tab}", uit)
                for tab in PROJECT_TABS
            )
            schermen.extend(open_dialoog(page, naam, args.base, project, aanroep, uit) for naam, aanroep in DIALOGEN)

        browser.close()

    met = [s for s in schermen if s.bevindingen]
    print(f"\n{len(schermen)} schermen bekeken, schermafbeeldingen in {uit}")
    print(f"{len(met)} met een bevinding\n")
    for scherm in met:
        print(f"## {scherm.naam}  ({scherm.url})")
        print(f"   plaatje: {scherm.plaatje}")
        for bevinding in scherm.bevindingen:
            print(f"   - {bevinding}")
        print()
    return 1 if met else 0


if __name__ == "__main__":
    sys.exit(main())
