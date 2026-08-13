"""Zet elk onderdeel van de webinterface twee keer op de foto: oud naast nieuw.

Waarom dit bestaat
------------------
De omzetting naar het nieuwe componentensysteem gaat per onderdeel, en de enige
controle die de echte fouten vond was kijken. Tot nu toe gebeurde dat met
wegwerpscripts: elke ronde een nieuw scriptje, een andere naamgeving, en niemand die
achteraf kon zeggen welk beeld bij welke stand hoorde. Dit bestand vervangt die
scripts door EEN inventaris.

Het draait tegen de lokale testserver (``tests/e2e/testserver.py``): de echte app met
gemockte buitenwereld. Geen cluster, geen deploy, geen Forgejo. Een ronde over alle
onderdelen kost ongeveer een minuut.

Gebruik
-------
    cd operations-manager/python

    uv run python scripts/lotc_shots.py --lijst          # welke onderdelen zijn er
    uv run python scripts/lotc_shots.py                  # alles, beide weergaven
    uv run python scripts/lotc_shots.py --alleen services,project-details
    uv run python scripts/lotc_shots.py --alleen wizard-stap-diensten --weergave nldd

De beelden landen in ``tests/e2e/screenshots/vergelijk/`` als
``<onderdeel>-<weergave>.png``, en er wordt een contactvel naast gezet:

    tests/e2e/screenshots/vergelijk/index.html

Dat vel toont de paren naast elkaar, met een schuif om ze over elkaar te leggen. Open
het in een browser; er hoeft niets voor te draaien.

Twee dingen die je moet weten voordat je een beeld gelooft
----------------------------------------------------------
1. NLDD is een webcomponentenlaag. Een screenshot die genomen wordt voordat de browser
   de custom elements heeft opgebouwd toont ongestileerde tekst, en dan klopt geen
   enkele afmeting. Vandaar ``_wacht_op_componenten``.
2. De oude weergave (``?layout=roos``) is de MAATSTAF, niet een tweede mening. Als het
   nieuwe beeld iets mist wat op het oude staat, is dat een fout - ook als het nieuwe
   beeld er beter uitziet.
"""

from __future__ import annotations

import argparse
import html
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from tests.e2e.conftest import TEST_USER, _sign_session  # noqa: E402
from tests.e2e.testserver import create_test_app  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

#: Het project dat de testserver meelevert; de projectpagina's hangen eraan.
PROJECT = "test-project-detail"

UITVOER = PYTHON_ROOT / "tests" / "e2e" / "screenshots" / "vergelijk"

BREEDTE = 1440
HOOGTE = 1000

WEERGAVEN = ("roos", "nldd")


# --------------------------------------------------------------------------- stappen
#
# Een stap is iets dat na het laden gedaan moet worden om het onderdeel in beeld te
# krijgen - een dialoog openen, een tabblad kiezen, een wizardstap doorlopen. Stappen
# worden in BEIDE weergaven uitgevoerd, dus ze mogen niet op markup leunen die maar aan
# een kant bestaat. Daarom is de voorkeursvorm een JS-aanroep: die is gedrag, en gedrag
# hoort identiek te zijn. Waar dat niet kan mag een selector per weergave.


@dataclass(frozen=True)
class Js:
    """Roep een functie van de pagina aan (bijv. ``openEditModal('modal-edit-identity')``)."""

    code: str
    wacht: float = 1.0

    def doe(self, page: Page, weergave: str) -> None:
        del weergave
        page.evaluate(self.code)
        page.wait_for_timeout(int(self.wacht * 1000))


@dataclass(frozen=True)
class Klik:
    """Klik iets aan. Een aparte selector per weergave mag, maar is een geurtje."""

    roos: str
    nldd: str | None = None
    wacht: float = 1.0

    def doe(self, page: Page, weergave: str) -> None:
        selector = self.roos if weergave == "roos" else (self.nldd or self.roos)
        page.locator(selector).first.click(timeout=10000)
        page.wait_for_timeout(int(self.wacht * 1000))


@dataclass(frozen=True)
class Vul:
    """Vul een veld, zodat een volgende stap verder kan."""

    selector: str
    waarde: str

    def doe(self, page: Page, weergave: str) -> None:
        del weergave
        page.locator(self.selector).first.fill(self.waarde, timeout=10000)


Stap = Js | Klik | Vul


# ------------------------------------------------------------------------ inventaris


@dataclass(frozen=True)
class Onderdeel:
    """Een ding dat op de foto moet. De naam is de bestandsnaam."""

    naam: str
    pad: str
    #: Waar dit onderdeel over gaat - komt op het contactvel te staan.
    wat: str = ""
    stappen: tuple[Stap, ...] = field(default_factory=tuple)
    #: Hele pagina of alleen wat in het venster past. Dialogen: venster.
    hele_pagina: bool = True
    hoogte: int = HOOGTE


ONDERDELEN: tuple[Onderdeel, ...] = (
    # --- de pagina's zelf
    Onderdeel("dashboard", "/dashboard", "startpagina met projecten en verbruik"),
    Onderdeel("projecten", "/projects", "projectenlijst; hier komen zoeken en sorteren"),
    Onderdeel("services", "/services", "servicekaarten: omschrijving, API-naam, variabelen"),
    Onderdeel("about", "/about", "over het platform"),
    Onderdeel("architectuur", "/architecture", "de architectuurpagina, 61 koppen en 8 diagrammen"),
    Onderdeel("metrics-explorer", "/metrics-explorer", "vrije metriekbevraging"),
    Onderdeel("admin-users", "/admin/users", "gebruikersbeheer"),
    Onderdeel("admin-approvals", "/admin/approvals", "goedkeuringen"),
    Onderdeel("admin-usage", "/admin/usage", "verbruik over alle projecten"),
    # --- de projectpagina, per tabblad
    Onderdeel("project-tab-project", f"/projects/{PROJECT}/details?tab=project", "projectgegevens"),
    Onderdeel("project-tab-deployments", f"/projects/{PROJECT}/details?tab=deployments", "deployments"),
    Onderdeel("project-tab-metrics", f"/projects/{PROJECT}/details?tab=metrics", "resourcegebruik"),
    Onderdeel("project-tab-taken", f"/projects/{PROJECT}/details?tab=taken", "takenlijst"),
    # --- de wizard
    Onderdeel("wizard-start", "/forms/wizard/start", "beginscherm van de wizard"),
)


def onderdeel_op_naam(naam: str) -> Onderdeel:
    for o in ONDERDELEN:
        if o.naam == naam:
            return o
    bekend = ", ".join(o.naam for o in ONDERDELEN)
    raise SystemExit(f"onbekend onderdeel: {naam}\nbekend: {bekend}")


# --------------------------------------------------------------------------- server


def _vrije_poort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_testserver() -> Iterator[str]:
    """Start de app op een vrije poort en geef zijn adres terug."""
    poort = _vrije_poort()
    ctx = create_test_app()
    with ctx() as app:
        config = uvicorn.Config(app, host="127.0.0.1", port=poort, log_level="warning")
        server = uvicorn.Server(config)
        draad = threading.Thread(target=server.run, daemon=True)
        draad.start()

        einde = time.monotonic() + 20
        while time.monotonic() < einde:
            try:
                with socket.create_connection(("127.0.0.1", poort), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"de testserver kwam niet op poort {poort}")

        try:
            yield f"http://127.0.0.1:{poort}"
        finally:
            server.should_exit = True
            draad.join(timeout=5)


# ------------------------------------------------------------------------ fotograaf


def _wacht_op_componenten(page: Page) -> None:
    """Wacht tot de browser elk custom element heeft opgebouwd.

    ``:not(:defined)`` selecteert precies de tags die de browser nog niet kent. Zolang
    er een over is toont een screenshot ongestileerde tekst en klopt geen enkele
    afmeting. Dit is de grootste bron van misleidende beelden bij een webcomponentenlaag.
    """
    page.wait_for_load_state("networkidle")
    try:
        page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)
    except Exception:
        print("      ! niet elk component was opgebouwd; het beeld kan afwijken")
    # De laatste opbouwslag (schaduwbomen, lettertypen) valt buiten networkidle.
    page.wait_for_timeout(400)


def _adres(basis: str, pad: str, weergave: str) -> str:
    scheiding = "&" if "?" in pad else "?"
    return f"{basis}{pad}{scheiding}layout={weergave}"


def fotografeer(page: Page, basis: str, onderdeel: Onderdeel, weergave: str) -> Path:
    page.set_viewport_size({"width": BREEDTE, "height": onderdeel.hoogte})
    page.goto(_adres(basis, onderdeel.pad, weergave), wait_until="domcontentloaded")
    _wacht_op_componenten(page)

    for stap in onderdeel.stappen:
        stap.doe(page, weergave)
    if onderdeel.stappen:
        _wacht_op_componenten(page)

    doel = UITVOER / f"{onderdeel.naam}-{weergave}.png"
    page.screenshot(path=str(doel), full_page=onderdeel.hele_pagina)
    return doel


# ----------------------------------------------------------------------- contactvel


CONTACTVEL_CSS = """
:root { color-scheme: light dark; --rand: #8884; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 24px; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.uitleg { margin: 0 0 24px; opacity: .75; max-width: 70ch; }
section { margin: 0 0 40px; border-top: 1px solid var(--rand); padding-top: 12px; }
h2 { font-size: 17px; margin: 0; }
h2 small { font-weight: normal; opacity: .6; margin-left: 8px; }
.paar { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px; }
figure { margin: 0; }
figcaption { font-size: 13px; opacity: .7; margin-bottom: 4px; }
img { width: 100%; border: 1px solid var(--rand); border-radius: 4px; display: block; }
.ontbreekt { border: 1px dashed var(--rand); border-radius: 4px; padding: 24px;
             text-align: center; opacity: .6; font-size: 13px; }
.over-elkaar { position: relative; margin-top: 12px; display: none; }
.over-elkaar img { position: absolute; inset: 0; }
.over-elkaar img.boven { clip-path: inset(0 var(--knip, 50%) 0 0); }
.over-elkaar .rek { position: relative; width: 100%; }
input[type=range] { width: 100%; margin-top: 8px; }
button.stand { font: inherit; padding: 2px 10px; margin-left: 12px; cursor: pointer; }
"""

CONTACTVEL_JS = """
document.querySelectorAll('section').forEach(sec => {
  const knop = sec.querySelector('button.stand');
  if (!knop) return;
  knop.addEventListener('click', () => {
    const laag = sec.querySelector('.over-elkaar');
    const paar = sec.querySelector('.paar');
    const aan = laag.style.display === 'block';
    laag.style.display = aan ? 'none' : 'block';
    paar.style.display = aan ? 'grid' : 'none';
    knop.textContent = aan ? 'over elkaar leggen' : 'naast elkaar';
  });
  const schuif = sec.querySelector('input[type=range]');
  if (schuif) schuif.addEventListener('input', e => {
    sec.querySelector('.over-elkaar').style.setProperty('--knip', (100 - e.target.value) + '%');
  });
});
"""


def schrijf_contactvel(onderdelen: tuple[Onderdeel, ...]) -> Path:
    stukken: list[str] = []
    for o in onderdelen:
        beelden = {w: UITVOER / f"{o.naam}-{w}.png" for w in WEERGAVEN}
        figuren = []
        for weergave in WEERGAVEN:
            pad = beelden[weergave]
            bijschrift = "oud (roos) - de maatstaf" if weergave == "roos" else "nieuw (nldd)"
            if pad.exists():
                figuren.append(
                    f"<figure><figcaption>{bijschrift}</figcaption>"
                    f'<img src="{pad.name}" alt="{html.escape(o.naam)} {weergave}"></figure>'
                )
            else:
                figuren.append(
                    f'<figure><figcaption>{bijschrift}</figcaption><div class="ontbreekt">geen beeld</div></figure>'
                )

        beide = all(p.exists() for p in beelden.values())
        laag = ""
        knop = ""
        if beide:
            knop = '<button class="stand">over elkaar leggen</button>'
            laag = (
                '<div class="over-elkaar"><div class="rek">'
                f'<img src="{beelden["roos"].name}" alt="oud">'
                f'<img class="boven" src="{beelden["nldd"].name}" alt="nieuw">'
                "</div><input type=range min=0 max=100 value=50></div>"
            )

        stukken.append(
            f"<section><h2>{html.escape(o.naam)}"
            f"<small>{html.escape(o.wat)} &middot; {html.escape(o.pad)}</small>{knop}</h2>"
            f'<div class="paar">{"".join(figuren)}</div>{laag}</section>'
        )

    doel = UITVOER / "index.html"
    doel.write_text(
        "<!doctype html><meta charset=utf-8>"
        "<title>LOTC: oud naast nieuw</title>"
        f"<style>{CONTACTVEL_CSS}</style>"
        "<h1>LOTC: oud naast nieuw</h1>"
        '<p class="uitleg">Links de oude weergave (roos) - dat is de maatstaf. Rechts de nieuwe (nldd). '
        "Mist rechts iets wat links staat, dan is dat een fout, ook als rechts er beter uitziet. "
        "Met de knop leg je de twee over elkaar en schuif je de grens heen en weer.</p>"
        + "".join(stukken)
        + f"<script>{CONTACTVEL_JS}</script>",
        encoding="utf-8",
    )
    return doel


# ------------------------------------------------------------------------------ cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lijst", action="store_true", help="toon de onderdelen en stop")
    parser.add_argument("--alleen", default="", help="komma-gescheiden namen; standaard alles")
    parser.add_argument("--weergave", default="", choices=["", *WEERGAVEN], help="maar een van de twee")
    args = parser.parse_args()

    if args.lijst:
        for o in ONDERDELEN:
            print(f"  {o.naam:28} {o.pad:52} {o.wat}")
        return 0

    gekozen = tuple(onderdeel_op_naam(n.strip()) for n in args.alleen.split(",") if n.strip()) or ONDERDELEN
    weergaven = (args.weergave,) if args.weergave else WEERGAVEN

    UITVOER.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    server = start_testserver()
    basis = next(server)
    print(f"testserver: {basis}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": BREEDTE, "height": HOOGTE})
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": _sign_session({"user": TEST_USER}),
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page = context.new_page()
            page.on("pageerror", lambda e: print(f"      ! JS-fout: {e}"))

            for o in gekozen:
                print(f"  {o.naam}")
                for weergave in weergaven:
                    try:
                        pad = fotografeer(page, basis, o, weergave)
                        print(f"      {weergave:5} -> {pad.relative_to(PYTHON_ROOT)}")
                    except Exception as fout:
                        print(f"      {weergave:5} MISLUKT: {type(fout).__name__}: {fout}")

            context.close()
            browser.close()
    finally:
        for _ in server:
            pass

    vel = schrijf_contactvel(ONDERDELEN)
    print(f"\ncontactvel: {vel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
