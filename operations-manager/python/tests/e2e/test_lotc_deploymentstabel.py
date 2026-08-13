"""De deploymenttabel op Overzicht is in de browser ECHT een tabel (RC-76).

DIT BESTAND BESTAAT OM EEN FOUT DIE GROEN WAS.

De vorige ronde zette dezelfde tabel neer met een browsertest erbij, en die was groen. Op
het scherm stond geen tabel: "Naam / Cluster / Status / Componenten" stonden onder elkaar
in plaats van naast elkaar, en elke rij daaronder ook. De oorzaak is dat NLDD van een
tabel een CSS-grid maakt: zonder het attribuut ``columns`` wordt
``grid-template-columns: none`` en valt elke cel op een eigen regel. De HTML klopte, de
tekst stond er, elke assertie op de markup was waar - en niemand had gekeken.

Daarom meet dit bestand GEOMETRIE en geen markup:

  - de koppen staan NAAST elkaar (zelfde regel, oplopende x);
  - er is precies EEN koprij;
  - een rij staat ONDER de koprij, en zijn cellen staan weer naast elkaar.

En er wordt een screenshot met meerdere rijen weggeschreven, om naar te kijken. Dat is
geen assertie; het is het beeld dat de vorige keer ontbrak.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
OVERZICHT = f"/projects/details/{PROJECT}"
SCREENSHOT_DIR = "tests/e2e/screenshots/lotc"

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900


def _wacht_op_nldd(page: Page) -> None:
    """Wacht tot de browser elk nldd-element heeft opgebouwd.

    Zolang er een ``*:not(:defined)`` over is, staat er ongestileerde tekst en zegt zowel
    een screenshot als een gemeten positie niets.
    """
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)


def _open_overzicht(page: Page, app_server: str) -> None:
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(f"{app_server}{OVERZICHT}")
    _wacht_op_nldd(page)


def _dozen(page: Page, selector: str) -> list[dict[str, float]]:
    """De posities van de elementen achter ``selector``, in documentcoordinaten."""
    return page.eval_on_selector_all(
        selector,
        """els => els.map(el => {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        })""",
    )


def test_de_koppen_staan_naast_elkaar_en_niet_onder_elkaar(app_server: str, auth_page: Page) -> None:
    """DE TOETS OP DE VORIGE FOUT.

    Onder elkaar betekent: elke kop een eigen regel, oplopende y en gelijke x. Naast
    elkaar betekent: gelijke y en oplopende x. Dat verschil is precies wat een assertie op
    de tekst niet ziet.
    """
    _open_overzicht(auth_page, app_server)

    koppen = _dozen(auth_page, '#deployments-lijst nldd-table [data-lotc-component="th"]')
    assert len(koppen) >= 4, f"de tabel heeft {len(koppen)} koppen"

    for eerder, later in pairwise(koppen):
        assert later["x"] > eerder["x"], "de koppen staan onder elkaar: dit is een lijst, geen tabel"
        assert abs(later["y"] - eerder["y"]) < eerder["h"], "de koppen staan niet op dezelfde regel"


def test_er_is_precies_een_koprij(app_server: str, auth_page: Page) -> None:
    _open_overzicht(auth_page, app_server)

    assert auth_page.locator('#deployments-lijst nldd-table [data-lotc-component="table-head"]').count() == 1


def test_een_rij_staat_onder_de_koprij_met_zijn_cellen_naast_elkaar(app_server: str, auth_page: Page) -> None:
    """Het testproject heeft twee deployments; beide horen als rij onder de koppen te
    staan, en niet als een stapel losse cellen."""
    _open_overzicht(auth_page, app_server)

    koppen = _dozen(auth_page, '#deployments-lijst nldd-table [data-lotc-component="th"]')
    rijen = auth_page.locator('#deployments-lijst nldd-table [data-lotc-component="table-row"]')
    assert rijen.count() == 2, f"twee deployments horen twee rijen te geven, niet {rijen.count()}"

    onderkant_koppen = max(kop["y"] + kop["h"] for kop in koppen)
    for index in range(rijen.count()):
        # Via de locator en niet met :nth-of-type: de koprij is OOK een <nldd-table-row>,
        # dus die telt in :nth-of-type mee en de eerste gegevensrij is dan nummer twee.
        cellen = (
            rijen.nth(index)
            .locator("nldd-cell")
            .evaluate_all(
                """els => els.map(el => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            })"""
            )
        )
        assert len(cellen) == len(koppen), "een rij heeft niet evenveel cellen als er koppen zijn"
        assert min(cel["y"] for cel in cellen) >= onderkant_koppen - 1, "een rij staat niet onder de koprij"
        for eerder, later in pairwise(cellen):
            assert later["x"] > eerder["x"], "de cellen van een rij staan onder elkaar"


def test_de_tabel_met_meerdere_rijen_op_beeld(app_server: str, auth_page: Page) -> None:
    """Een screenshot om NAAR TE KIJKEN, met meer rijen dan de fixture heeft.

    De extra rijen zijn klonen van de laatste rij, in de browser gemaakt: het gaat hier om
    de vorm bij twintig deployments, en dat is precies waarvoor deze tabel de kaarten
    verving. De assertie eronder is smal (de tabel wordt hoger maar niet breder); het beeld
    is het punt.
    """
    _open_overzicht(auth_page, app_server)

    voor = auth_page.locator("#deployments-lijst nldd-table").bounding_box()
    assert voor is not None

    auth_page.evaluate(
        """() => {
            const tabel = document.querySelector('#deployments-lijst nldd-table');
            const rijen = tabel.querySelectorAll('[data-lotc-component="table-row"]');
            const laatste = rijen[rijen.length - 1];
            for (let i = 0; i < 8; i++) tabel.appendChild(laatste.cloneNode(true));

            // De statuskolom gevuld, want de testopstelling heeft geen ArgoCD en toont
            // daar dus streepjes. Het gaat om de VORM van een gevulde kolom: passen drie
            // labels naast elkaar, of duwen ze de kolom uit elkaar? Dit is opmaak voor het
            // beeld en geen bewering over de applicatie - de labels zelf staan getoetst in
            // tests/test_lotc_deploymentstabel.py.
            const standen = [
                [['Healthy', 'success'], ['Synced', 'success']],
                [['slaapstand', 'accent'], ['Synced', 'success']],
                [['Degraded', 'critical'], ['OutOfSync', 'warning']],
            ];
            tabel.querySelectorAll('[data-lotc-component="table-row"]').forEach((rij, index) => {
                const cel = rij.querySelectorAll('nldd-cell')[2];
                if (!cel) return;
                // Dezelfde wikkel als het sjabloon (<c-cluster gap="xs">), anders meet je
                // de vorm van iets dat de pagina niet bouwt.
                const wikkel = document.createElement('div');
                wikkel.className = 'lotc-cluster lotc-cluster--align-center';
                wikkel.setAttribute('data-lotc-component', 'cluster');
                wikkel.style.setProperty('--lotc-cluster-gap', '0.5rem');
                for (const [tekst, kleur] of standen[index % standen.length]) {
                    const tag = document.createElement('nldd-tag');
                    tag.setAttribute('text', tekst);
                    tag.setAttribute('color', kleur);
                    wikkel.appendChild(tag);
                }
                cel.innerHTML = '';
                cel.appendChild(wikkel);
            });
        }"""
    )
    _wacht_op_nldd(auth_page)

    auth_page.locator("#deployment-status").screenshot(
        path=f"{SCREENSHOT_DIR}/bg-deploymentstabel.png", animations="disabled"
    )

    na = auth_page.locator("#deployments-lijst nldd-table").bounding_box()
    assert na is not None
    assert na["height"] > voor["height"], "acht rijen erbij maakten de tabel niet hoger"
    assert abs(na["width"] - voor["width"]) < 2, "de tabel werd breder van rijen erbij"


def test_de_deployment_staat_op_zijn_tabblad_maar_een_keer(app_server: str, auth_page: Page) -> None:
    """De twee blokken zijn samengevoegd (RC-76).

    Er stond een statuskaart met naam en cluster, en daaronder het paneel
    "Deployment: <naam>" met dezelfde naam in de kop. Nu is er EEN blok: de naam staat een
    keer, en de statusgegevens staan erin.

    Gemeten op de KOPTEKST en niet op een klasse: het gaat erom wat een lezer twee keer
    zag staan.
    """
    auth_page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    auth_page.goto(f"{app_server}/projects/deployments/{PROJECT}/default")
    _wacht_op_nldd(auth_page)

    zichtbaar = auth_page.locator("#deployment-default")
    koppen = zichtbaar.locator("nldd-heading, h2, h3, h4").all_text_contents()
    noemt_de_naam = [kop for kop in koppen if "default" in kop]
    assert len(noemt_de_naam) == 1, f"de naam staat {len(noemt_de_naam)} keer boven een blok: {noemt_de_naam}"

    zichtbaar.screenshot(path=f"{SCREENSHOT_DIR}/bg-deploymentpaneel.png", animations="disabled")


def test_de_statuskaarten_staan_er_niet_meer_naast(app_server: str, auth_page: Page) -> None:
    """De tabel VERVANGT het blok met een kaart per deployment. Twee weergaven van
    dezelfde lijst naast elkaar lopen uiteen, en dat was de opdracht niet.
    """
    _open_overzicht(auth_page, app_server)

    assert auth_page.locator("#deployments-lijst nldd-table").count() == 1
    assert auth_page.locator("[id^='deployment-status-']").count() == 0, (
        "de losse statuskaarten staan nog naast de tabel"
    )


def test_de_rij_opent_de_deployment_op_zijn_eigen_tabblad(app_server: str, auth_page: Page) -> None:
    """De tabel is de ingang; het detail staat op het tabblad Deployments. Een gewone link,
    dus dit werkt ook zonder JavaScript en is deelbaar."""
    _open_overzicht(auth_page, app_server)

    auth_page.locator(f'#deployments-lijst nldd-table a[href$="/deployments/{PROJECT}/tweede"]').first.click()
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"/projects/deployments/{PROJECT}/tweede")
    assert auth_page.locator("#deployment-tweede").count() == 1
    assert auth_page.locator("#deployment-default").count() == 0, "de andere deployment staat er ook nog"


def test_de_kiezer_benoemt_de_deployment_die_open_staat(app_server: str, auth_page: Page) -> None:
    """De kiezer volgt het PAD.

    Welke deployment de pagina toont staat in de URL (/projects/deployments/<p>/<naam>).
    De kiezer bleef eerder op de eerste optie staan, en dat is twee keer fout: hij benoemt
    een andere deployment dan er open staat, en een native <select> vuurt geen change als
    je de al getoonde optie kiest - waardoor die deployment via de kiezer niet meer te
    bereiken was.

    De WAARDE van een optie is sinds RC-92 het adres van die deployment: kiezen is
    navigeren.
    """
    auth_page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    auth_page.goto(f"{app_server}/projects/deployments/{PROJECT}/tweede")
    _wacht_op_nldd(auth_page)

    kiezer = auth_page.locator("#global-deployment-selector")
    assert auth_page.locator("#deployment-tweede").count() == 1
    assert kiezer.input_value().endswith(f"/projects/deployments/{PROJECT}/tweede"), (
        "de kiezer benoemt een andere deployment dan er open staat"
    )

    # En terug: 'default' kiezen verandert de waarde ECHT, dus de change vuurt en de
    # browser haalt die pagina op.
    kiezer.select_option(f"/projects/deployments/{PROJECT}/default")
    auth_page.wait_for_url(f"**/projects/deployments/{PROJECT}/default", timeout=5000)
    _wacht_op_nldd(auth_page)
    assert auth_page.locator("#deployment-default").count() == 1
    assert auth_page.locator("#deployment-tweede").count() == 0


def test_zoeken_werkt_via_de_url(app_server: str, auth_page: Page) -> None:
    """Server-side: de URL draagt de keuze, dus het werkt zonder JavaScript en een
    gefilterde lijst is deelbaar."""
    auth_page.goto(f"{app_server}{OVERZICHT}?q=tweede")
    _wacht_op_nldd(auth_page)

    rijen = auth_page.locator('#deployments-lijst nldd-table [data-lotc-component="table-row"]')
    assert rijen.count() == 1
    assert "tweede" in rijen.first.inner_text()


def test_sorteren_werkt_via_de_url(app_server: str, auth_page: Page) -> None:
    auth_page.goto(f"{app_server}{OVERZICHT}?dsort=naam-af")
    _wacht_op_nldd(auth_page)

    namen = auth_page.eval_on_selector_all(
        '#deployments-lijst nldd-table [data-lotc-component="table-row"] nldd-cell:first-of-type',
        "els => els.map(e => e.innerText.trim())",
    )
    assert namen == sorted(namen, reverse=True), f"aflopend sorteren leverde {namen}"


# ------------------------------------------------------------------ de tabblad-URL's


def test_elk_tabbladadres_toont_zijn_eigen_tabblad(app_server: str, auth_page: Page) -> None:
    """Het PAD bepaalt welk tabblad je ziet, en niets anders."""
    for pad, wikkel in (
        ("details", "#tab-project"),
        ("componenten", "#tab-componenten"),
        ("services", "#tab-services"),
        ("deployments", "#tab-deployments"),
        ("metrics", "#tab-metrics"),
        ("taken", "#tab-taken"),
    ):
        auth_page.goto(f"{app_server}/projects/{pad}/{PROJECT}")
        auth_page.wait_for_load_state("networkidle")
        assert auth_page.locator(wikkel).count() == 1, f"/projects/{pad}/ toont {wikkel} niet"


def test_elk_tabblad_heeft_zijn_eigen_adres(app_server: str, auth_page: Page) -> None:
    """De tabbalk is een rij echte links naar echte paden."""
    _open_overzicht(auth_page, app_server)

    adressen = auth_page.eval_on_selector_all(
        "nldd-tab-bar a, nldd-tab-bar-item a", "els => els.map(e => new URL(e.href).pathname)"
    )
    assert f"/projects/deployments/{PROJECT}" in adressen
    assert f"/projects/componenten/{PROJECT}" in adressen
    assert not [adres for adres in adressen if "?" in adres]
