# Overdracht: de omzetting naar het nieuwe componentensysteem afmaken

Branch `naar-het-nieuwe-componentensysteem`. Laatste uitrol op de sandbox: `7e1f3978`.
Dit vervangt `plans/lotc-restwerk.md`.

---

## 1. Waar dit over gaat

De webinterface van ZAD gaat van **jinja-roos-components** (RVO) naar **Lord of the
Components** met het **NLDD**-thema. De nieuwe weergave is inmiddels de standaard; de oude
is bereikbaar met `?layout=roos`.

**De opdracht, letterlijk zoals de gebruiker hem stelde:**

> "je moet gewoon zorgen dat het hetzelfde blijft doen maar er gewoon anders uitziet"

Niet herontwerpen. Niet weglaten omdat je iets overbodig vindt. Niet toevoegen omdat je
het mooier vindt. Als je denkt dat iets beter kan: **noteer het, doe het niet.**

Wat er misging in de eerste ronde was steeds hetzelfde: er werd ontworpen waar overgezet
moest worden, en de controle achteraf keek naar de verkeerde dingen. Concreet wat dat
kostte:

| wat er gebeurde | gevolg |
|---|---|
| infoknop werd een inline link i.p.v. een venster | gebruiker moest het melden |
| deploymentkiezer weggelaten | idem |
| formulier in een kolom van 46rem gezet ("leesbaarder") | halve breedte, las als mobiele weergave |
| servicekaart verloor omschrijving + omgevingsvariabelen | idem |
| "Services" hernoemd naar "Diensten" | idem |
| knop kreeg tekst als kind i.p.v. `label=` | knop toonde alleen een plusje |
| aanvinkvakje bleef een webcomponent | **selectie werkte niet; formulier stuurde ALLES** |
| onze eigen CSS-variabelen niet meegenomen | **dialoog volledig doorzichtig** |

De laatste twee zijn de belangrijkste les: **geen enkele geautomatiseerde controle vond
ze.** De HTML klopte, de attributen klopten, de adressen klopten.

---

## 2. De werkwijze (van de gebruiker, en hij werkt)

Per onderdeel, in deze volgorde. Niet doorgaan naar het volgende onderdeel voordat dit
onderdeel af is.

1. **Wat is er nu.** Open het originele sjabloon. Maak een screenshot van de oude pagina.
   Noteer: welke blokken, welke knoppen, welke teksten, welk gedrag.
2. **Hoe zet ik dat om.** Welk component wordt wat. Welke id's/klassen draagt JavaScript
   (die neem je LETTERLIJK over, dat is geen vormgeving).
3. **Omzetten.**
4. **Visueel checken.** Screenshot maken en **zelf bekijken**. Naast het origineel leggen.
5. **Functioneel checken.** In de browser klikken, en meten wat er gebeurt: wat wordt er
   verstuurd, wat verandert er in de DOM.
6. **Bij een verschil: terug naar 3.** Niet "later oppakken".

Stap 4 en 5 zijn de stappen die overgeslagen werden. Alles wat de gebruiker vond, zat daar.

---

## 3. De testaanpak: vier lagen, want elke laag is blind voor iets anders

### Laag 1 — Compileren
`tests/test_lotc_conversion.py`. LOTC valideert bij het compileren of elk component en elk
attribuut bestaat. Vangt typefouten en verzonnen attributen.
**Blind voor:** alles wat daarna gebeurt.

### Laag 2 — Gedragspariteit
`scripts/lotc_compare_behaviour.py` en `tests/e2e/test_lotc_parity.py`. Haalt dezelfde
route twee keer op (`?layout=roos` en `?layout=nldd`) en vergelijkt: bestemmingen,
htmx-adressen, aangeroepen JS-functies, invoervelden, id's. Meldt ook **achterstand**:
hoeveel oude markup er nog in de nieuwe weergave zit.

```bash
uv run python scripts/lotc_compare_behaviour.py \
  --base https://zad.sandbox.rijksapp.dev \
  --secret "$(kubectl -n rig-system exec deploy/operations-manager -- \
              python -c 'from opi.core.config import settings; print(settings.SECRET_KEY)')" \
  --email <adres met toegang> --project <projectnaam>
```

**Blind voor:** of iets WERKT. Een knop die niets doet en een vakje dat niet aanvinkt zijn
hier volkomen schoon.
**Bekende gaten in dit gereedschap (nog te repareren):** velden die als
`<nldd-*-field name=...>` renderen worden niet herkend; `hx-target` wordt niet vergeleken
(daardoor bleef een kapot doel `#metrics-content` aan beide kanten staan); van
JS-aanroepen wordt alleen de NAAM vergeleken en niet de argumenten (daardoor werd een
ontbrekende knop "Projectgegevens bewerken" gemist).

### Laag 3 — Zien
Screenshot maken en met je ogen bekijken, naast het origineel. Recept:

```python
auth_page.set_viewport_size({"width": 1440, "height": 1000})
auth_page.goto(f"{app_server}{pad}?layout=nldd")
auth_page.wait_for_load_state("networkidle")
# ZONDER dit klopt geen enkele afmeting: NLDD-componenten zijn webcomponenten
auth_page.wait_for_function("() => !document.querySelector('*:not(:defined)')")
auth_page.screenshot(path=...)
```

Dit vond: de doorzichtige dialoog, de halve breedte, velden die aan elkaar plakten, de
knop zonder tekst, de voettekst tegen de knoppen.
`tests/e2e/test_lotc_breedte.py` automatiseert één klasse hiervan (inhoud die in een te
smalle kolom belandt).

### Laag 4 — Uitvoeren
Klikken en meten wat de browser echt doet. Twee recepten die je nodig gaat hebben:

**Onderschep wat er verstuurd wordt:**
```python
posts = []
page.on("request", lambda r: posts.append((r.url, r.post_data or "")) if r.method == "POST" else None)
```

**Zoek besturingselementen ook in de schaduwboom** (Playwright doet dat wel, gewone
JavaScript niet — en de applicatie gebruikt gewone JavaScript):
```javascript
() => { const uit = []; const loop = (w) => { w.querySelectorAll('*').forEach(e => {
  if (e.matches('input[type=checkbox]')) uit.push({n: e.name, aan: e.checked, schaduw: w !== document});
  if (e.shadowRoot) loop(e.shadowRoot); }); }; loop(document); return uit; }
```

Dit vond de ergste fout: klikken op een servicekaart vinkte niets aan, en het formulier
verstuurde vervolgens **alle veertien** services in plaats van geen enkele.

### De suite draaien

```bash
cd operations-manager/python
uv run pytest tests/ -p no:randomly                    # 6739 groen
uv run pytest tests/e2e -m "e2e and not sandbox" -q    # browser
```

**Val niet in deze val:** géén eigen `-m` meegeven aan de gewone suite. De
standaardinstelling sluit `requires_infra` uit; met een eigen `-m` haal je die weer binnen
en wacht de suite een half uur op een database die er niet is.

**Het cijfer dat er het meest toe doet:** `E2E_LAYOUT=nldd uv run pytest tests/e2e -m "e2e
and not sandbox"` → **39 van de 286 falen**. De bestaande e2e-tests zijn op de oude markup
geschreven en staan daarom via een cookie in `tests/e2e/conftest.py` op `roos` gepind.
Zolang dat zo is, bewaakt niets wat gebruikers echt krijgen. **Dit is post 1 hieronder.**

---

## 4. Wat er nog moet gebeuren, op volgorde

### 1. De e2e-suite groen op de nieuwe weergave — 39 tests
Zet `E2E_LAYOUT=nldd` en werk ze af. Ze wijzen naar: de bewerkdialogen (`test_edit_wizard`),
de deployments (`test_detail_page`, `test_hidden_at_load`), de bevestigingen
(`test_detail_confirmations`), backup/restore (`test_backup`) en vier wizardstappen.
Verwacht twee soorten oorzaken: selectors die op oude markup staan (test aanpassen) en
echt kapot gedrag (applicatie aanpassen). **Onderscheid die twee zorgvuldig** — een test
"groen maken" door de selector te verruimen terwijl de functie stuk is, is precies hoe dit
project in de problemen kwam.

### 2. Servicepagina: de omgevingsvariabelen (HALF AF)
Het origineel (`opi/templates/services-overview/_diensten.html.j2`) toont per service:
omschrijving, "API naam: `<code>`", en een blok **Omgevingsvariabelen** met per variabele
de naam, de aliassen en de uitleg. De nieuwe kaart toont daarvan niets; de variabelen
werden alleen geTELD op een chip ("3 variabelen").
- `opi/web/lotc_switch.py` levert `variables` inmiddels aan — **gedaan**.
- Nog doen: `bg/_service-card.html.j2` moet omschrijving, API-naam en het variabelenblok
  tonen. Mag in drie kolommen (verzoek van de gebruiker).
- Kijk ook naar `used_by` en `chips` in `lotc_switch.services_overview`: die staan niet in
  het origineel. `used_by` is bovendien altijd leeg — dode code.

### 3. Projectenpagina: zoeken en sorteren
Gevraagd, niet begonnen. De gebruiker leverde de gewenste vorm aan: een `nldd-toolbar` met
een zoekveld (`slot="start"`) en een sorteerknop met uitklapmenu (`slot="end"`), plus een
`nldd-menu-group slot="overflow"` voor smalle schermen. **Alles via htmx**, zoals de rest
van dit project. Het zoekfilter dat er ooit stond werkte niet en is verwijderd.

### 4. Gebruikersmenu rechtsboven
Gevraagd, niet begonnen. Uitklapbaar menu met de naam van de gebruiker, met daarin:
profiel, **weergave (systeem / licht / donker)**, een beheer-submenu en uitloggen. De
gebruiker leverde de gewenste structuur aan (`nldd-menu-bar-item expandable` met een
geneste `nldd-menu`).
Gemeten en bruikbaar: `c-menu type="bar"` + `c-menu-item expandable` + geneste `c-menu` +
`c-menu-divider` werken. `type="radio"` en `selected` kent `c-menu-item` niet als
attribuut; die gaan via `:attrs="{...}"`.
**Het thema schakelt via `data-scheme` op `<html>`** (`light`/`dark`; niets = systeem, zie
`settings.css` van NLDD). Onthouden in localStorage en zetten vóór de eerste weergave,
anders flitst de pagina.

### 5. Vijf blokken in dialogen tonen nog de oude vormgeving
`FormRenderer._render_layout_element` rendert `TemplatePartial` en `DisplayBlock` **altijd**
via de roos-omgeving (`opi/forms/renderer.py:917` en `:926`), ongeacht de adapter. Raakt
`modal-edit-domain` (24 oude componenten), `modal-edit-attachments` (8) en `modal-restore`
(3) — en de wizardpagina net zo goed. Ook `router_wizard_attachments.py` (2 rendersites)
staat nog vast op roos.

### 6. Knop "Projectgegevens bewerken" ontbreekt
Het origineel heeft hem (`project-details/section-header.html.j2` regel 9,
`openEditModal('modal-edit-identity', ...)`). Op de nieuwe projectpagina is er geen enkele
weg naar `modal-edit-identity`.

### 7. De metrics-pagina werkt niet goed
Door de gebruiker gemeld, **niet onderzocht**. Begin met de gedragsvergelijking en daarna
laag 3 en 4. Let op: `bg/_deployment-metrics.html.j2` is recent herschreven (canvassen
terug, tekencode verhuisd naar `static/js/metrics_charts.js`) en dat is niet in een echte
browser met echte metingen beproefd.

### 8. Kleine dingen die op screenshots te zien zijn
- Op de componentdialoog zweeft "Docker image van je applicatie..." boven het veld in
  plaats van eronder (zie `scratchpad/popup.png`).
- Het thema markeert **optionele** velden ("Optioneel") waar het oude thema de
  **verplichte** met een rood sterretje markeerde. De informatie is er, in de omgekeerde
  conventie. Bewust zo gelaten; als de gebruiker de sterretjes terug wil, is dat een
  bewuste wijziging.
- De fieldsets van het oude thema waren grijze blokken; in NLDD zijn het koppen zonder
  omhulsel. Groepering is zwakker.

### 9. Meetlat bijwerken
De drie gaten uit paragraaf 3, laag 2.

### 10. Architectuurpagina
1509 regels in één blok. **Als laatste**, op uitdrukkelijk verzoek van de gebruiker. `/`
verwijst hiernaartoe, dus de startpagina komt hiermee mee.

### 11. De oude weergave eruit
Pas als post 1 op nul staat. Dan kunnen de oude templates, de schakelaar (`?layout=`, de
`zad_layout`-cookie) en `jinja-roos-components` weg — omdat ze aantoonbaar niet meer nodig
zijn, niet omdat we hopen dat het goed komt. **Let op:** de gedragsvergelijking (laag 2)
werkt alleen zolang beide weergaven bestaan. Die verdwijnt dus tegelijk; zorg dat laag 3
en 4 dan op orde zijn.

---

## 5. Valkuilen van dit componentensysteem

Deze zijn allemaal duur betaald. Lees ze voordat je begint.

**Attributen die stilzwijgend verdwijnen.** Een component neemt alleen de attributen aan
die in zijn definitie staan; de rest wordt weggegooid zonder foutmelding. Zo verdween
`value` op een keuzelijst, `href` op een kaart, en `class` op de dialoog. Toets dat een
attribuut echt in de uitvoer landt.

**Kinderen die stilzwijgend verdwijnen.** `<c-button>`, `<c-icon-button>`, `<c-toolbar>` en
`<c-byline>` hebben geen standaard-slot: tekst als KIND wordt weggegooid. Gebruik `label=`
of een named slot. Dit kostte "Item toevoegen" en "Verwijderen" hun tekst.

**`@click` overleeft de omzetting niet.** De omzetter las `@click="f()"` half en gooide het
weg — 58 keer in 35 bestanden, knoppen die renderen en zwijgen. Sinds
`scripts/lotc_convert_templates.py` er `:attrs="{'onclick': ...}"` van maakt is dat opgelost;
schrijf het met de hand ook zo, met de aanroep in een `{% set %}` BOVEN de tag (een genest
aanhalingsteken binnen `:attrs` breekt de tag).

**Geen Jinja binnen een componenttag.** `{% if %}` of een `{# commentaar #}` binnen
`<c-...>` leest de voorbewerker als attribuutnaam. Gebruik `:prop="expr or none"`.

**Besturingselementen in de schaduwboom.** Dit is de gevaarlijkste. `c-checkbox` wordt een
webcomponent dat zijn `<input>` in de schaduwboom zet. `static/js/wizard.js` zoekt met
`querySelector('input[type="checkbox"]')` en vindt niets; het formulier serialiseert de
lichte boom en stuurt de verkeerde waarden. Vier widgets hebben daarom nu een gewoon
`<input>` (servicekaarten, preset-kaarten, aanvinkgroep, los vakje).
Voor keuzelijsten bestaat `native="true"` — gebruik dat overal waar JavaScript de lijst
vult of uitleest. **Vraag het LOTC-project om `native` op het aanvinkvakje**; dan kunnen
die vier terug naar een component.

**Onze eigen CSS-variabelen bestaan niet in het nieuwe thema.** 368 plekken in onze eigen
stylesheets verwijzen naar `--rvo-*`, en een lege waarde negeert de browser volledig.
Daarom was de dialoog doorzichtig. De 31 variabelen die echt bestaan staan nu gedefinieerd
in `static/css/lotc-app.css`. **Dat bestand hoort te krimpen**: elke keer dat een van onze
stylesheets vervangen wordt door wat het thema levert, kan er een regel weg.

**`c-layout-flow` zet geen tussenruimte.** Het wordt een `<nldd-container>` zonder gap.
Gebruik `c-stack` (dat is `.lotc-stack`, `display:flex` met een echte gap).

**Twee componentsystemen kunnen niet in één Jinja-omgeving.** De eerst geregistreerde
voorbewerker eist elke `<c-*>`-tag op. De grens loopt per overervingsketen. Voor blokken
die een SERVICE zelf levert is er `render_roos()` in `opi/core/templates_lotc.py`: die
rendert zo'n sjabloon met zijn eigen omgeving. Zichtbaar anders is beter dan ongemerkt weg.

**De ontwerpsystemen staan in een vaste volgorde:** `["lotc-layout", "nldd", "lotc-forms"]`.
`lotc-forms` moet achteraan, anders lossen de invoervelden niet op.

---

## 6. Afspraken die al gemaakt zijn

- Het heet **Services**, niet "Diensten".
- Aanspreekvorm is **je**, niet "u" — ook in teksten die als string in Python staan.
  `tests/test_lotc_schrijfwijze.py` bewaakt dat, inclusief `opi/forms/`.
- **Repositories** hoeft niet getoond te worden.
- Het **resourcegebruik** staat op een eigen tabblad Metrics.
- De waarschuwingsbalk bovenaan is weg: dit is de applicatie, geen proefopstelling.
- De sandbox is van ons; de vergrendeling blijft staan (`sandbox-release` pas als het klaar
  is).

---

## 7. Praktisch

**Uitrollen naar de sandbox:** `sandbox-deploy` (bouwt uit `/workspace`, rolt uit, en
controleert `/version`). Bij "WARN — /version does not clearly show ..." is de oude pod nog
aan het antwoorden; even wachten en opnieuw kijken.

**Een sessiecookie voor metingen op de sandbox:**
```python
sleutel = <SECRET_KEY uit de draaiende pod>
payload = base64.b64encode(json.dumps({"user": {"sub": "x", "email": "<adres>", "name": "X"}}).encode()).decode()
cookie = itsdangerous.TimestampSigner(sleutel).sign(payload).decode()
```
Het adres moet op de allowlist staan, anders krijg je de "geen toegang"-pagina terug — en
die rendert prima, dus een meting die dat niet doorheeft meet niets. Op de sandbox werkt
`admin@sandbox.rijksapp.dev`.

**Twee metingen die niets zeggen** (allebei zelf gemaakt en pas laat opgemerkt): een
meting die op elke pagina hetzelfde antwoord geeft, kijkt naar de loginpagina. En een
gedragsvergelijking die "0 verdwenen" zegt over een blok dat in BEIDE weergaven de oude
vormgeving toont, zegt alleen dat er niets verdwenen is — niet dat het omgezet is. Kijk
altijd ook naar de achterstandsmeting.
