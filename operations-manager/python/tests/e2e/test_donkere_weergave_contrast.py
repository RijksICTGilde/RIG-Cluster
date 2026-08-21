"""Is de donkere weergave leesbaar? Gemeten, niet bekeken (RC-134).

De melding was "grijze tekst op een donkergrijze achtergrond, zo flauw dat labels en
waarden bijna wegvallen". Zo'n melding is niet met een blik op de HTML af te doen: de
markup klopte overal, en de kleur die je ziet komt uit een keten van themavariabelen,
schaduwbomen en onze eigen stijlbladen. Deze test rekent daarom uit wat de gebruiker
werkelijk ziet: per stuk tekst de BEREKENDE kleur, de achtergrond die eronder ligt
(desnoods dwars door schaduwbomen en slots heen), en de contrastverhouding daartussen.

De norm is WCAG AA: 4,5:1 voor gewone tekst, 3:1 voor grote tekst (>= 24px, of >= 18,66px
en vet). Beide weergaven worden gemeten, want een reparatie voor donker mag licht niet
slopen.

WAT ER GEMETEN WERD EN WAT ERUIT KWAM (voor deze reparatie, in de donkere weergave):

  wizard, samenvattingstap        #333333 op #20252B  1,22   van ons (wizard.css), 39 stuks
                                  #555555 op #20252B  2,07
  wizard, dienstenstap            14 witte .service-card-vlakken van 400x115
  projectpagina, "Config&Secrets" #FFFFFF op #FFFFFF  1,00   componentenlaag (c-secret-field)
  bewerkdialoog, kop en labels    #D9DEE5 op #FFFFFF  1,35   van ons (modal.css)
  bewerkdialoog, "Laden..."       #64748B op #FFFFFF  3,24   van ons (modal.css)
  /introductie, "Naar zad-cli"    #154273 op #121212  1,84   componentenlaag (shortcut-cta)
  /admin/usage, de filterlabels   #1A1A1A op #121212  1,08   componentenlaag + onze spiegel

De lichte weergave was overal in orde, en is dat na de reparatie nog steeds - dat is de
reden dat hij hier meemeet.

DE VOLGORDE VAN REPAREREN DOET ERTOE, en dat kostte hier een ronde. De dienstkaarten
stonden als vast WIT vlak met vaste DONKERE tekst: samen leesbaar, maar een fel wit blok in
een donkere pagina. Alleen het vlak naar het thema brengen maakte het erger - toen stond de
vaste donkere tekst op het donkere themavlak, 1,36:1. Voorgrond en achtergrond horen in
dezelfde stap mee.

WAT NIET REPRODUCEERDE. De melding noemde ook het VOORTGANGSSCHERM van een taak (de
stappenlijst en de kop "Voortgang"). Dat scherm is volledig van thema-componenten gemaakt
en meet in de donkere weergave 15,4:1 tot 18,7:1 - er was daar niets te repareren. Het
staat hieronder toch mee in de meting, zodat die vaststelling houdbaar blijft.

DE ACHTERGROND KOMT NIET UIT `background-color` VAN DE PAGINA. Met `color-scheme: dark`
tekent de browser zelf een donker vlak (#121212 in Chromium) en blijft de computed
`background-color` van <html> doorzichtig. Wie dus omhoog loopt tot hij iets tegenkomt,
eindigt op wit en meet contrast 1,00 op elk element van de pagina - een meting die alleen
maar ruis oplevert. De ondergrond hieronder komt daarom van een proefelement met
`background-color: Canvas`: dat IS de kleur die de browser tekent, in beide standen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opi.core.templates_lotc import templates_lotc
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: Een projectfixture met config-blok, deployments en diensten (tests/e2e/fixtures).
PROJECT = "test-project-services"

#: De meting, in de browser. Per element met eigen tekst: de kleur, de opgestapelde
#: achtergrond en de verhouding daartussen.
#:
#: Kleuren worden door een canvas gehaald in plaats van met een reguliere expressie
#: gelezen. Het thema levert zijn kleuren als ``oklch(...)``, en daar rekent geen
#: rgb-parser mee; een test die die waarden overslaat meet stilletjes een fractie van de
#: pagina. Verven en de PIXEL teruglezen werkt voor elke notatie die CSS kent.
METING = """() => {
  const proef = document.createElement('div');
  proef.style.backgroundColor = 'Canvas';
  document.documentElement.appendChild(proef);
  const canvas = getComputedStyle(proef).backgroundColor;
  proef.remove();

  const ctx = document.createElement('canvas').getContext('2d');
  const parse = (s) => {
    if (!s) return null;
    if (s === 'transparent' || s === 'none') return {rgb: [0, 0, 0], a: 0};
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = s;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return {rgb: [d[0], d[1], d[2]], a: d[3] / 255};
  };
  const lum = (c) => {
    const [r, g, b] = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const over = (voor, achter) => voor.rgb.map((v, i) => v * voor.a + achter[i] * (1 - voor.a));
  const verhouding = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const wortelBg = (parse(canvas) || {rgb: [255, 255, 255]}).rgb;

  const uit = [];
  const loop = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) loop(el.shadowRoot);
      const eigen = [...el.childNodes]
        .filter(n => n.nodeType === 3 && n.textContent.trim())
        .map(n => n.textContent.trim()).join(' ');
      if (!eigen) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      const doos = el.getBoundingClientRect();
      if (doos.width < 1 || doos.height < 1) continue;

      // Omhoog door de PLATGESLAGEN boom: een geslot element hangt onder zijn <slot>,
      // niet onder zijn eigen ouder, en de vlakken zitten juist in die schaduwboom.
      const stapel = [];
      let n = el;
      while (n) {
        const c = parse(getComputedStyle(n).backgroundColor);
        if (c && c.a > 0) { stapel.push(c); if (c.a === 1) break; }
        const wortel = n.getRootNode();
        n = n.assignedSlot || n.parentElement || (wortel && wortel.host) || null;
      }
      let bg = wortelBg;
      for (let i = stapel.length - 1; i >= 0; i--) bg = over(stapel[i], bg);

      const voor = parse(cs.color);
      if (!voor) continue;
      const groot = parseFloat(cs.fontSize) >= 24
        || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight) >= 700);
      uit.push({
        tekst: eigen.slice(0, 60),
        tag: el.tagName.toLowerCase(),
        color: cs.color,
        bg: 'rgb(' + bg.map(Math.round).join(', ') + ')',
        fontSize: cs.fontSize,
        groot: groot,
        ratio: Math.round(verhouding(over(voor, bg), bg) * 100) / 100,
      });
    }
  };
  loop(document);
  return uit;
}"""

#: De voortgang van een taak is zonder takendienst niet via een route te bereiken, dus
#: wordt het fragment - in zijn eigen paneel, zoals de pagina hem toont - server-side
#: gerenderd en in een echte pagina van de testserver gezet. Zelfde thema, zelfde
#: componenten, zelfde stijlbladen als de pagina die de gebruiker krijgt.
VOORTGANG_CONTEXT: dict[str, Any] = {
    "task_id": "t1",
    "project_name": PROJECT,
    "progress": 40,
    "current_step": "Bezig",
    "tasks": [
        {
            "name": "Project bijwerken",
            "status": "completed",
            "error": None,
            "subtasks": [{"name": "YAML wegschrijven", "status": "running", "subtasks": [], "error": None}],
        },
        {"name": "Uitrollen", "status": "failed", "error": "Kon niet verbinden", "subtasks": []},
    ],
    "status": "running",
    "error": None,
    "progress_url": "/x",
    "container_id": "c1",
}

#: Een vlak dat in de DONKERE weergave duidelijk lichter is dan het paginavlak.
#:
#: Waarom dit naast de contrastmeting staat: niet elke kleur die de stand negeert levert
#: onleesbare tekst op. Het filterformulier van /admin/usage en de keuzelijsten van
#: lotc-forms zetten hun vlak op een vaste #FFFFFF EN hun tekst op een vaste #1A1A1A;
#: die combinatie haalt de norm ruim, en toch staat er een fel wit blok midden in een
#: donkere pagina. Dat is precies wat de melding beschreef, en het is met een
#: contrastverhouding niet te vangen.
#:
#: De drempels: het vlak moet zelf licht zijn (luminantie >= 0,5) en duidelijk lichter dan
#: het paginavlak (verschil >= 0,4). Een gedempt vlak dat in donker iets lichter is dan de
#: pagina - wat elke kaart doet - blijft daar ruim onder. Voor de reparatie vond dit er 9,
#: erna 0.
LICHTE_EILANDEN = """() => {
  const ctx = document.createElement('canvas').getContext('2d');
  const parse = (s) => {
    if (!s || s === 'transparent' || s === 'none') return {rgb: [0, 0, 0], a: 0};
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = s; ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return {rgb: [d[0], d[1], d[2]], a: d[3] / 255};
  };
  const lum = (c) => {
    const [r, g, b] = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const proef = document.createElement('div');
  proef.style.backgroundColor = 'Canvas';
  document.documentElement.appendChild(proef);
  const paginaLum = lum(parse(getComputedStyle(proef).backgroundColor).rgb);
  proef.remove();

  const uit = [];
  const loop = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) loop(el.shadowRoot);
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      const doos = el.getBoundingClientRect();
      if (doos.width < 20 || doos.height < 10) continue;
      // De uitzondering voor de CodeMirror-editor (klassen cm-*) IS WEG. Die stond hier
      // omdat de editor zijn kantlijn en zijn sleutelnamen uit zijn eigen, vaste lichte
      // basisthema tekende - een wit vlak in een donkere pagina. Hij heeft nu een thema
      // uit onze --semantics-tokens (static/js/codemirror-kv.js), dus hij hoort gewoon
      // meegemeten te worden; anders zou het terugvallen van dat thema hier stil zijn.
      const c = parse(cs.backgroundColor);
      if (c.a < 0.9) continue;
      const l = lum(c.rgb);
      if (l < 0.5 || l - paginaLum < 0.4) continue;
      uit.push({
        tag: el.tagName.toLowerCase(),
        klasse: (el.getAttribute('class') || '').slice(0, 60),
        bg: cs.backgroundColor,
        w: Math.round(doos.width),
        h: Math.round(doos.height),
      });
    }
  };
  loop(document);
  return uit;
}"""

VOORTGANG_PANEEL = (
    '{% from "bg/_patterns.html.j2" import panel %}'
    '{% call panel("Voortgang", "timer") %}{% include "bg/_task-progress.html.j2" %}{% endcall %}'
)


def _zet_weergave(page: Page, weergave: str) -> None:
    """De licht/donker-stand zoals de gebruiker hem zet: het koekje dat base_lotc leest."""
    page.context.add_cookies([{"name": "zad_scheme", "value": weergave, "domain": "127.0.0.1", "path": "/"}])


def _keur(page: Page, waar: str, weergave: str) -> None:
    page.wait_for_timeout(400)
    gemeten = page.evaluate(METING)
    assert gemeten, f"{waar} ({weergave}): geen enkel stuk tekst gemeten - de meting zelf is stuk"

    onvoldoende = [r for r in gemeten if r["ratio"] < (3.0 if r["groot"] else 4.5)]
    assert onvoldoende == [], "\n".join(
        [f"{waar} ({weergave}): {len(onvoldoende)} van {len(gemeten)} stukken tekst halen WCAG AA niet:"]
        + [
            f"  {r['ratio']}:1  {r['color']} op {r['bg']} ({r['fontSize']}, <{r['tag']}>)  {r['tekst']!r}"
            for r in onvoldoende
        ]
    )

    if weergave != "dark":
        return
    eilanden = page.evaluate(LICHTE_EILANDEN)
    assert eilanden == [], "\n".join(
        [
            f"{waar} (dark): {len(eilanden)} vlak(ken) staan als LICHT eiland in een donkere "
            "pagina - een kleur die de licht/donker-stand niet volgt:"
        ]
        + [f"  <{e['tag']} class={e['klasse']!r}> {e['bg']}  {e['w']}x{e['h']}" for e in eilanden]
    )


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_projectpagina_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """Inclusief "Configuratie & Secrets", waar de secret-velden wit op wit stonden."""
    _zet_weergave(auth_page, weergave)
    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.wait_for_load_state("networkidle")
    _keur(auth_page, "projectpagina", weergave)


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_bewerkdialoog_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """De gedeelde dialoog: het vlak kwam uit onze eigen CSS en volgde de stand niet."""
    _zet_weergave(auth_page, weergave)
    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.wait_for_load_state("networkidle")
    auth_page.locator("text=Bewerken").first.click()
    auth_page.locator(".edit-section-modal.is-open").wait_for(state="visible", timeout=15000)
    auth_page.wait_for_load_state("networkidle")
    _keur(auth_page, "bewerkdialoog", weergave)


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_introductie_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """De verwijsblokken onderaan, waarvan de link op een vaste donkerblauwe kleur stond."""
    _zet_weergave(auth_page, weergave)
    auth_page.goto(f"{app_server}/introductie")
    auth_page.wait_for_load_state("networkidle")
    _keur(auth_page, "introductie", weergave)


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_gebruik_en_kosten_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """Het filterformulier, met de labels die op een vaste donkere tekstkleur stonden."""
    _zet_weergave(auth_page, weergave)
    auth_page.goto(f"{app_server}/admin/usage")
    auth_page.wait_for_load_state("networkidle")
    _keur(auth_page, "gebruik en kosten", weergave)


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_de_wizard_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """Elke stap van de aanmaakwizard, tot en met de samenvatting.

    De opdrachtgever bakende het af op de FORMULIEREN, en daar zat het ook: de
    samenvattingstap had 39 stukken tekst onder de norm en de dienstenstap veertien witte
    vlakken. Geen enkele daarvan was te zien op de losse pagina's - de stappen zitten achter
    een flow die je moet doorlopen, dus een sweep over URL's komt er niet.

    Er wordt per stap gemeten en niet alleen op het eind: elke stap heeft eigen velden en
    eigen blokken, en de samenvatting toont weer een andere opmaak dan de invoerstappen.
    """
    _zet_weergave(auth_page, weergave)
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    _keur(auth_page, "wizard-stap projectgegevens", weergave)

    wizard.fill_identity(display_name=unique_project_name("contrast"), description="contrastmeting")
    for _ in range(_MAX_WIZARD_STAPPEN):
        stap = auth_page.url.rsplit("/step/", 1)[-1]
        if stap == "team":
            wizard.fill_team(email="test@example.com")
        elif stap == "components":
            wizard.fill_component(name="web", image="nginx:1.25")
        wizard.click_next()
        auth_page.wait_for_load_state("networkidle")
        _keur(auth_page, f"wizard-stap {auth_page.url.rsplit('/step/', 1)[-1]}", weergave)
        if auth_page.locator("#wizard-review-submit").count() > 0:
            return
    raise AssertionError(f"de samenvatting is niet bereikt binnen {_MAX_WIZARD_STAPPEN} stappen")


#: Vangnet, geen wachtmechanisme: elke geselecteerde dienst voegt zijn eigen configuratiestap
#: toe, dus het aantal stappen ligt niet vast. De lus stopt op de samenvatting.
_MAX_WIZARD_STAPPEN = 15


#: Legt bij de EERSTE component-upgrade vast wat er op <html> stond. Wordt geinstalleerd
#: voordat er iets van de pagina draait.
HAAK_EERSTE_UPGRADE = """
window.__eersteUpgrade = null;
window.__eersteNaam = null;
const origineel = customElements.define.bind(customElements);
customElements.define = function (naam, klasse, opties) {
  const proto = klasse.prototype;
  const oud = proto.connectedCallback;
  proto.connectedCallback = function () {
    if (window.__eersteUpgrade === null) {
      window.__eersteUpgrade = document.documentElement.getAttribute('data-scheme');
      window.__eersteNaam = naam;
    }
    if (oud) return oud.apply(this, arguments);
  };
  return origineel(naam, klasse, opties);
};
"""


def test_data_scheme_staat_er_voor_het_eerste_component_upgradet(app_server: str, auth_page: Page) -> None:
    """Onze omweg om <c-page> heen is niet te laat, en dus geen oorzaak.

    ``<c-page>`` laat geen attribuut op ``<html>`` toe, dus zet base_lotc.html.j2 de stand
    met een inline ``<script>``. Een web-component dat zijn kleuren bij upgrade uitrekent en
    daarna niet herrekent, zou dan de lichte waarden zien - dezelfde vorm als de
    ``width=``-fout in request_for_components.md. Gemeten is dat het niet zo is: het script
    staat in de ``<head>`` en de componenten staan in de ``<body>``, dus de stand is er al.
    Deze test houdt dat zo, want een verhuizing van dat script zou het stil omdraaien.
    """
    auth_page.context.add_cookies([{"name": "zad_scheme", "value": "dark", "domain": "127.0.0.1", "path": "/"}])
    auth_page.add_init_script(HAAK_EERSTE_UPGRADE)
    auth_page.goto(f"{app_server}/projects/")
    auth_page.wait_for_load_state("networkidle")

    stand, naam = auth_page.evaluate("() => [window.__eersteUpgrade, window.__eersteNaam]")

    assert naam, "geen enkel component is geupgraded - de haak of de pagina is stuk"
    assert stand == "dark", (
        f"bij de upgrade van <{naam}> stond data-scheme op {stand!r} in plaats van 'dark'. "
        "Het script dat de stand zet, staat dan te laat: componenten die hun kleuren bij "
        "upgrade uitrekenen, zien de lichte waarden."
    )


def test_systeem_donker_geeft_hetzelfde_als_expliciet_donker(app_server: str, auth_page: Page) -> None:
    """Bij "systeem" zet base_lotc GEEN data-scheme; dan hangt alles aan prefers-color-scheme.

    Dat is een tweede weg naar dezelfde stand, en als onze omweg iets zou breken, zou het
    verschil juist hier zichtbaar zijn. Gemeten: de twee wegen leveren op de projectpagina
    hetzelfde beeld op, voor en na de reparatie.
    """
    auth_page.emulate_media(color_scheme="dark")
    auth_page.goto(f"{app_server}/projects/test-project-services/details")
    auth_page.wait_for_load_state("networkidle")
    _keur(auth_page, "projectpagina met systeem-donker", "dark")


@pytest.mark.parametrize("weergave", ["dark", "light"])
def test_voortgang_van_een_taak_is_leesbaar(app_server: str, auth_page: Page, weergave: str) -> None:
    """Het scherm uit de melding dat NIET stuk bleek; dat blijft zo meetbaar."""
    _zet_weergave(auth_page, weergave)
    auth_page.goto(f"{app_server}/projects/")
    auth_page.wait_for_load_state("networkidle")

    paneel = templates_lotc.env.from_string(VOORTGANG_PANEEL).render(**VOORTGANG_CONTEXT)
    auth_page.evaluate(
        "(html) => { const bak = document.createElement('div'); bak.id = 'meetbak';"
        " bak.innerHTML = html; document.querySelector('main, body').appendChild(bak); }",
        paneel,
    )
    auth_page.wait_for_timeout(600)
    _keur(auth_page, "voortgang van een taak", weergave)
