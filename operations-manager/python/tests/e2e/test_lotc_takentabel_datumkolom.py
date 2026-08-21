"""Staat de datum in de takentabel op EEN regel, in een browser?

De kolommen Gestart en Beeindigd braken hun datum af over meerdere regels. Dat is
geometrie: het hangt aan de verdeling in ``columns`` van ``<c-table>``, en die verdeling
gaf die twee kolommen elk ``1fr`` -- het smalst van de zes, terwijl er de langste vaste
waarde in staat. Een assertie op de HTML ziet daar niets van, dus wordt het hier gemeten
zoals in tests/e2e/test_lotc_voortgangslijst_beeld.py: het fragment wordt server-side
gerenderd en in een ECHTE pagina van de testserver gezet, zodat de thema-CSS en de
componenten precies die van de applicatie zijn.

De inhoud is met opzet het ONGUNSTIGSTE geval dat de tabel kan tonen: de langste
soortnaam ("Deployment verwijderen"), de langste status ("Wordt gestart"), een echt
e-mailadres in Door, en een tijdstip in de langste maand ("18 sep 2026 01:40").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.web.router_tasks import _normalize_run, _normalize_task

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: De breedtes waarop dit beoordeeld wordt. 1280 is de gangbare laptop, 1440 een ruimer
#: scherm; 1024 is de smalste waarop de tabel nog als tabel bedoeld is.
GANGBAAR = [1280, 1440]

#: De smalste breedte waarop de tabel nog als tabel bedoeld is. Daar past niet alles meer;
#: wat er dan wel moet gelden staat in de laatste test.
SMAL = 1024

#: Elke breedte waarop gemeten wordt.
BREEDTES = [SMAL, *GANGBAAR]

#: Het project dat de testserver op de detailpagina toont.
PROJECT = "test-project-detail"

ITEMS: list[dict[str, Any]] = [
    _normalize_task(
        {
            "task_id": "11111111-2222-3333-4444-555555555555",
            "task_type": "delete_deployment",
            "status": "starting",
            "deployment_name": "deployment-1",
            "created_by": "robbert.uittenbroek@rijksoverheid.nl",
            "created_at": "2026-09-17T21:18:55.951682+00:00",
            "completed_at": None,
        }
    ),
    _normalize_run(
        {
            "kind": "db-console",
            "status": "stopped",
            "deployment": "deployment-1",
            "started_by": "robbert.uittenbroek@rijksoverheid.nl",
            "started_at": "2026-09-17T21:18:55+00:00",
            "ended_at": "2026-09-17T23:40:01+00:00",
        }
    ),
]

#: Per cel: hoeveel REGELS de tekst inneemt, hoe breed hij is en hoe breed hij zou zijn
#: als hij niet mocht afbreken.
#:
#: Het regelaantal komt uit ``Range.getClientRects()`` over de tekst zelf. Dat is de
#: enige meting die afbreken echt vaststelt: de celhoogte vergelijken werkt niet, want
#: alle cellen in een rij zijn even hoog zodra er ergens iets afbreekt.
#:
#: ``gebruikt`` is de breedte die de tekst FEITELIJK inneemt. Groter dan ``breedte``
#: betekent dat hij buiten zijn eigen kolom staat en dus over de buurman heen loopt --
#: een cel knipt niets af.
METING = """(fragment) => {
    const bak = document.querySelector('#tab-taken');
    bak.innerHTML = fragment;
    return customElements.whenDefined('nldd-table').then(() => new Promise(klaar => {
        requestAnimationFrame(() => requestAnimationFrame(() => {
            const uit = [];
            for (const rij of bak.querySelectorAll('nldd-table-row')) {
                const cellen = [];
                for (const cel of rij.querySelectorAll('nldd-cell')) {
                    const tekst = cel.textContent.trim();
                    const bereik = document.createRange();
                    bereik.selectNodeContents(cel);
                    const regels = new Set();
                    for (const r of bereik.getClientRects()) {
                        if (r.width > 0 || r.height > 0) regels.add(Math.round(r.top));
                    }
                    const c = cel.getBoundingClientRect();
                    cellen.push({
                        tekst: tekst,
                        regels: tekst ? Math.max(regels.size, 1) : 0,
                        breedte: Math.round(c.width),
                        gebruikt: Math.round(bereik.getBoundingClientRect().width),
                        nodig: (() => {
                            const oud = cel.style.whiteSpace;
                            cel.style.whiteSpace = 'nowrap';
                            const b = Math.round(bereik.getBoundingClientRect().width);
                            cel.style.whiteSpace = oud;
                            return b;
                        })(),
                    });
                }
                if (cellen.length) uit.push(cellen);
            }
            bak.innerHTML = '';
            klaar(uit);
        }));
    }));
}"""

#: De volgorde van de kolommen, voor leesbare foutmeldingen.
KOLOMMEN = ["Soort", "Deployment", "Status", "Door", "Gestart", "Beeindigd"]


def _meet(app_server: str, auth_page: Page, breedte: int) -> list[list[dict[str, Any]]]:
    auth_page.set_viewport_size({"width": breedte, "height": 900})
    auth_page.goto(f"{app_server}/projects/taken/{PROJECT}")
    auth_page.wait_for_load_state("networkidle")
    auth_page.wait_for_function("() => !document.querySelector('*:not(:defined)')")
    fragment = templates_lotc.env.get_template("bg/_tasks.html.j2").render(
        request=None, project_name="va-48w", items=ITEMS
    )
    return auth_page.evaluate(METING, fragment)


@pytest.mark.parametrize("breedte", GANGBAAR)
def test_de_datum_staat_op_een_regel(app_server: str, auth_page: Page, breedte: int) -> None:
    """Gestart en Beeindigd breken niet af op de gangbare breedtes.

    Dit is de meting waar de reparatie om begonnen is. Voor de reparatie stond er
    "2026-09-17 21:18" over drie of vier regels: het UTC-getal, afgekapt op zestien
    tekens, in de smalste kolom van de tabel.
    """
    rijen = _meet(app_server, auth_page, breedte)

    assert len(rijen) == 3, f"verwacht een koprij en twee gegevensrijen, kreeg {len(rijen)}"
    for rij in rijen[1:]:
        for naam, cel in zip(KOLOMMEN, rij, strict=True):
            if naam not in ("Gestart", "Beeindigd"):
                continue
            assert cel["regels"] == 1, (
                f"{breedte}px: '{cel['tekst']}' in {naam} beslaat {cel['regels']} regels "
                f"(kolom {cel['breedte']}px, nodig {cel['nodig']}px)"
            )


#: Kolommen die op de gangbare breedtes NIET mogen afbreken, met per uitzondering de reden.
#:
#: Soort en Door zijn de twee die het wel mogen. "Deployment verwijderen" is 163px en past
#: bij 1280 sowieso niet, ook in de oude verdeling niet; het zijn twee woorden, dus daar is
#: afbreken ook wat je verwacht. Door bevat een e-mailadres van 251px, en dat past in geen
#: enkele verdeling van deze tabel - dat het daar afbreekt is precies de bedoeling van de
#: klasse lange-waarden-breken.
MAG_NIET_AFBREKEN = ("Deployment", "Status", "Gestart", "Beeindigd")


@pytest.mark.parametrize("breedte", GANGBAAR)
def test_de_ruimte_kwam_niet_van_een_kolom_die_hem_nodig_had(app_server: str, auth_page: Page, breedte: int) -> None:
    """De datumkolommen kregen ruimte, maar niet ten koste van een kolom die past.

    Zonder deze test is de reparatie een verschuiving: je krijgt de datum op een regel
    door een andere kolom te laten afbreken, en dan is het probleem verplaatst in plaats
    van opgelost.
    """
    rijen = _meet(app_server, auth_page, breedte)

    afbrekend = [
        f"{naam}: '{cel['tekst']}' over {cel['regels']} regels ({cel['breedte']}px, nodig {cel['nodig']}px)"
        for rij in rijen
        for naam, cel in zip(KOLOMMEN, rij, strict=True)
        if cel["regels"] > 1 and naam in MAG_NIET_AFBREKEN
    ]
    assert afbrekend == [], f"{breedte}px: deze kolommen breken af: {afbrekend}"


@pytest.mark.parametrize("breedte", BREEDTES)
def test_geen_kolom_loopt_over_zijn_buurman_heen(app_server: str, auth_page: Page, breedte: int) -> None:
    """Ook op 1024px staat elke waarde binnen zijn eigen kolom.

    Een cel knipt niets af, dus een woord dat niet past loopt gewoon over de volgende
    kolom heen. Het e-mailadres in Door deed dat over de datum in Gestart, die daardoor
    onleesbaar was - het viel pas op toen de datum in die kolom paste. De klasse
    ``lange-waarden-breken`` (static/css/lotc-app.css) laat zo'n woord breken; deze test
    is wat die klasse vastpint, op ELKE breedte, want juist op 1024px is er niets over.

    Gemeten wordt de TEKST in een cel. De cel Status bevat geen tekst maar een ``c-tag``,
    en dat is een vast blokje dat niet kan afbreken: bij 1024px is het label 102px in een
    kolom van 68 en steekt het uit. Dat is niet met een kolomverdeling op te lossen (dan
    zou Status daar een kwart van de tabel moeten krijgen) en het is er niet slechter op
    geworden - in de oude verdeling was diezelfde kolom 52px. Het hoort bij het component
    en niet bij deze reparatie.
    """
    rijen = _meet(app_server, auth_page, breedte)

    buiten = [
        f"{naam}: '{cel['tekst']}' is {cel['gebruikt']}px in een kolom van {cel['breedte']}px"
        for rij in rijen
        for naam, cel in zip(KOLOMMEN, rij, strict=True)
        if cel["tekst"] and cel["gebruikt"] > cel["breedte"] + 1
    ]
    assert buiten == [], f"{breedte}px: deze waarden staan buiten hun kolom: {buiten}"
