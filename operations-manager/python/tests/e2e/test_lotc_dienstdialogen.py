"""De dialogen die een DIENST levert, in de browser: doen de knoppen nog iets?

Het tweede van de vijf gaten uit de vorige ronde was een knop die er wel stond maar
nergens op stond. Dit bestand gaat over het nauwere geval dat daaruit volgde: een knop die
er staat, er goed uitziet, en niets DOET.

De jobdialoog verloor zijn knop drie keer achter elkaar, en geen enkele keer werd er iets
rood:

1. ``<c-button type="submit">`` - op ``c-button`` is ``type`` de VORMGEVING
   (primary/secondary) en heet het HTML-attribuut ``html-type``. De component schreef zelf
   ``type="button"``.
2. ``html-type="submit"`` in een echte ``<form>`` - de klik van een ``<nldd-button>``
   bereikt het submit-event van de omliggende form niet. Zelfs ``form.requestSubmit()``
   leverde geen verzoek op, terwijl het formulier geldig was.
3. De knop met ``hx-post`` plus ``hx-include`` op de wikkel - dan vertrok het verzoek wel,
   maar met alleen ``deployment=...``. Een NLDD-veld is een form-associated
   web-component: zijn waarde komt in de FormData van een formulier terecht, maar het echte
   ``<input>`` zit in de shadow root en is onzichtbaar voor de DOM-query van htmx.

**Waarom de suite zweeg.** Alle drie de keren stond de bedrading in de markup goed en was
hij in beide vormgevingen gelijk: het ``hx-post`` zat op het formulier, en de vergelijking
van gedragsoppervlakken zag dus geen verschil. Wat er niet gemeten werd, is of een KLIK dat
verzoek ook echt afvuurt, en met welke gegevens.

Daarom wordt hier geklikt en wordt het VERZOEK afgelezen: het adres en wat erin zit. Het
verzoek wordt onderschept, dus er wordt niets gestart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
URL = f"/projects/details/{PROJECT}?tab=deployments"


def _vang(page: Page, patroon: str) -> list[Request]:
    """Onderschep verzoeken naar *patroon* en laat ze niet door."""
    gevangen: list[Request] = []

    def handler(route: Route, request: Request) -> None:
        gevangen.append(request)
        route.abort()

    page.route(patroon, handler)
    return gevangen


def test_de_jobdialoog_stuurt_de_ingevulde_image_mee(app_server: str, auth_page: Page) -> None:
    """Klikken op "Job uitvoeren" vuurt het verzoek af MET de image erin.

    Het adres alleen is niet genoeg: de knop vuurde een tijdlang keurig zijn POST af met
    uitsluitend de deploymentnaam erin, waarna de server hetzelfde lege formulier
    terugstuurde en de dialoog onveranderd bleef. Dat ziet eruit als "er gebeurt niets".
    """
    auth_page.goto(f"{app_server}{URL}")
    auth_page.wait_for_load_state("networkidle")

    auth_page.get_by_text("Job uitvoeren", exact=True).first.click()
    auth_page.locator("#job-modal-body").wait_for(state="attached", timeout=10000)

    verzoeken = _vang(auth_page, "**/jobs")
    auth_page.locator("input[id^='job-image-']").first.fill("voorbeeld/image:1")
    auth_page.locator("#job-modal-body nldd-button").first.click()
    auth_page.wait_for_timeout(2000)

    assert verzoeken, "de knop 'Job uitvoeren' vuurde geen enkel verzoek af"
    payload = verzoeken[0].post_data or ""
    assert "image=voorbeeld" in payload.replace("%2F", "/"), (
        f"het verzoek ging weg zonder de ingevulde image: {payload!r}. "
        f"Een NLDD-veld is een form-associated web-component; hx-include moet op de VELDEN "
        f"wijzen (nldd-text-field), niet alleen op de wikkel eromheen."
    )


def test_de_consoledialoog_vuurt_zijn_verzoek_af(app_server: str, auth_page: Page) -> None:
    """Dezelfde meting voor de databaseconsole: klikken, en het verzoek aflezen."""
    auth_page.goto(f"{app_server}{URL}")
    auth_page.wait_for_load_state("networkidle")

    auth_page.get_by_text("Databaseconsole", exact=True).first.click()
    auth_page.locator("#db-console-modal-body").wait_for(state="attached", timeout=10000)

    verzoeken = _vang(auth_page, "**/db-console")
    auth_page.locator("#db-console-modal-body nldd-button").first.click()
    auth_page.wait_for_timeout(2000)

    assert verzoeken, "de knop 'Console starten' vuurde geen enkel verzoek af"
    assert "deployment=" in (verzoeken[0].post_data or "")
