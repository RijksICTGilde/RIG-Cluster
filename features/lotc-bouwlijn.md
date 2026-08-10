# LOTC-bouwlijn: OPI op Lord of the Components

De omzetting van de webinterface naar **Lord of the Components** (LOTC) met het
NLDD-thema, ter vervanging van `jinja-roos-components`. De nieuwe vormgeving is inmiddels
de STANDAARD; de oude is er nog als terugvaloptie.

Achtergrond en fasering: `plans/naar-het-nieuwe-componentensysteem.md`.
De samenleefmeting: `docs/lotc-samenleven-met-jinja-roos.md`.

## Wat het is

- **Echte routes** die hun pagina al door LOTC kunnen renderen: `/services`,
  `/dashboard`, `/projects` en `/projects/details/<naam>` (met het resourcegebruik-
  fragment dat htmx apart inlaadt), plus de beheerpagina's `/admin/users` (inclusief het
  formulier op `/admin/users/create` en `/admin/users/<id>/edit`), `/admin/approvals` en
  `/admin/usage`, en **de wizard** (`/forms/wizard/start`, `/forms/wizard/<flow>`,
  `/forms/wizard/<flow>/edit/<project>`, elke htmx-stap en de samenvatting). Zet er
  `?layout=roos` achter voor de oude weergave.
- Daarnaast een **proefopstelling** onder `/lotc/`, met voorbeeldprojecten. Die is er om
  vorm te kiezen zonder een cluster nodig te hebben, niet als eindbestemming.
- De navigatie volgt de opzet van [bg.rijks.app](https://bg.rijks.app/): hoofdnavigatie
  in een zijkolom met groepen, alleen hulplinks (account, in- en uitloggen) in de header.
- De formulierlaag is te bekijken op `/lotc/formulier`.

**Welke weergave je krijgt** bepaalt `opi/web/lotc_switch.py`, in deze volgorde:

1. `?layout=nldd` of `?layout=roos` in de URL,
2. anders de cookie `zad_layout`, die alleen gezet wordt als je expliciet gekozen hebt,
3. anders de standaard, en dat is **nldd**.

Lord of the Components is een gewone runtime-dependency: de applicatie rendert er
pagina's mee, dus hij hoort in de image.

## Gebruik

```bash
cd operations-manager/python
uv sync
uv run python scripts/lotc_convert_templates.py     # templates opnieuw omzetten
uv run pytest tests/test_lotc_conversion.py tests/test_lotc_icon_mapping.py -q
uv run pytest tests/e2e/test_lotc_visual.py -m "e2e and not sandbox" -q
```

De screenshots landen in `tests/e2e/screenshots/lotc/` en staan in de repo: ze zijn het
resultaat waar de omzetting op beoordeeld wordt, niet een wegwerpartefact.

Draaiende applicatie:

| pad | wat |
|---|---|
| `/lotc/` | de schil, zonder pagina-inhoud |
| `/lotc/pagina/<naam>` | een omgezette pagina, bijvoorbeeld `dashboard` |
| `/lotc/formulier` | elk veldtype, uit voorbeeldvelden |
| `/static/lotc/...` | de CSS en JS van de actieve design systems |

## Hoe het in elkaar zit

| bestand | rol |
|---|---|
| `opi/core/templates_lotc.py` | de tweede Jinja-omgeving, met design systems, globals en filters |
| `opi/templates_lotc/` | de omgezette templates - **gegenereerd**, niet met de hand bewerken |
| `opi/templates_lotc/base_lotc.html.j2` | de schil in bg-opzet - **wel** met de hand |
| `opi/templates_lotc/widgets/` | de formulierwidgets - **wel** met de hand |
| `opi/templates_lotc/bg/` | de hertekende pagina's - **wel** met de hand |
| `opi/templates_lotc/bg/_patterns.html.j2` | gedeelde patronen: `panel()`, `page_head()`, `info()`, `service_card()` |
| `opi/templates_lotc/bg/wizard-page.html.j2`, `wizard-start.html.j2`, `_wizard-step.html.j2`, `_wizard-steps.html.j2`, `_wizard-review.html.j2` | de wizard: de hele pagina, de startpagina, en de drie fragmenten die htmx wisselt |
| `opi/web/lotc_switch.py` | de schakelaar waarmee een echte route zijn weergave kiest |
| `opi/web/lotc_fixtures/` | voorbeeldprojecten voor de proefopstelling |
| `opi/web/navigation_lotc.py` | de indeling van de navigatie en de icoonvertaling |
| `opi/web/lotc_router.py` | de routes onder `/lotc/` |
| `opi/forms/widgets/lotc.py` | de widget-adapter die de LOTC-templates rendert |
| `opi/forms/lotc_attrs.py` | de attribuutbundel van een veld, voor LOTC's `:attrs` |
| `scripts/lotc_convert_templates.py` | de omzetter |

### Twee omgevingen, geen twee design systems

Beide componentsystemen registreren een Jinja-extensie die de bron voorbewerkt en **elke**
`<c-*>`-tag opeist. In één `Environment` claimt de eerst geregistreerde voorbewerker alles
en breekt op de tags die hij niet kent; een doorlaatstand bestaat aan geen van beide
kanten. Twee losse omgevingen werken wel.

De grens loopt daarbij niet per pagina maar **per overervingsketen**: een template dat
`base_lotc.html.j2` uitbreidt wordt door de LOTC-omgeving gerenderd, een template dat
`base.html.j2` uitbreidt door de roos-omgeving. Mengen binnen een keten kan niet.

### Een omzetter, geen handwerk

De release blijft aan diezelfde templates werken. Een met de hand overgetypte kopie van
152 bestanden is vanaf dag twee verouderd zonder dat iemand ziet waar. Daarom genereert
`scripts/lotc_convert_templates.py` de inhoud van `opi/templates_lotc/`, en zit het
handwerk in de vertaalregels.

Twee soorten templates vallen daarbuiten en staan er met de hand, omdat er een keuze aan
te pas komt die een script niet kan maken: de schil (de indeling verandert, niet alleen
de namen) en de formulierwidgets (welk attribuut waar hoort, per widget).

Wijzig je iets in `opi/templates/`, draai dan de omzetter opnieuw.

### Wat er anders is aan LOTC

Bij het omzetten komen steeds dezelfde vier dingen terug:

1. **Attributen zijn kebab-case** (`body-class`, `max-width`), niet camelCase.
2. **Samenstellingen krijgen kinderen, geen data-props.** `<c-menu>` met `<c-menu-item>`
   in plaats van `:items="..."`.
3. **Jinja mag niet op attribuutpositie.** Waar roos-templates een macro een stuk
   attribuut-tekst midden in de tag lieten schrijven, gebruikt LOTC `:prop="expr"` -
   `none` betekent weglaten - en `:attrs="<dict>"` voor een hele bundel.
4. **`<c-page>` bedraadt de `<head>` zelf**, inclusief de CSS en JS van elk actief design
   system onder `/static/lotc/`.

### De volgorde van de design systems ligt vast

```python
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]
```

`lotc-forms` hoort achteraan, ná het visuele thema, anders lossen de invoervelden niet
op. `lotc-layout` is formeel opt-in maar in de praktijk vereist: de structuurprimitieven
renderen uitsluitend via dat pakket.

### Componenten die wij zelf leveren

Nu geen. Er stond een eigen `c-secret-field` omdat LOTC die niet had; sinds hun versie er
is, is die weg. De opruiming was wat we ervan hoopten: het registratieblok weg, het
template weg, geen paginawijziging - de aanroepvorm was al die van hen.

Komt het opnieuw voor, dan is de weg: `merge_fragment` op de registry, plus de eigen
templatemap op de `searchpath`. Twee regels, en twee regels om weer weg te halen.

## Testen

| test | bewaakt |
|---|---|
| `tests/test_lotc_conversion.py` | dat elk template compileert, en dat de lijst uitzonderingen niet groeit |
| `tests/test_lotc_icon_mapping.py` | dat elke icoonvertaling naar een bestaande NLDD-naam wijst |
| `tests/e2e/test_lotc_visual.py` | dat pagina's in een browser kloppen, met screenshots |
| `tests/test_lotc_layout_rules.py` | dat kaarten via `panel()` gebouwd worden en gaps uit de schaal komen |
| `tests/test_lotc_schrijfwijze.py` | dat teksten de lezer met "je" aanspreken |
| `tests/test_lotc_switch.py` | dat de volgorde URL > cookie > standaard klopt |
| `tests/e2e/test_lotc_parity.py` | dat de nieuwe pagina alles KAN wat de oude kon |

Compileren is een echte poort en geen telling: LOTC valideert bij het compileren al of
elk component bestaat en of elk attribuut bij dat component hoort.

**De screenshottests zijn niet optioneel.** Twee fouten in de formulierlaag zagen er in
de HTML goed uit en waren in de browser stuk: een keuzelijst zonder opties (een browser
gooit binnen een `<select>` alles weg wat geen `<option>` is) en een aanvinkvakje zonder
besturingselement. Geen compileertest of HTML-assertie ving die.

Ze wachten expliciet tot de NLDD-webcomponenten door de browser zijn opgebouwd
(`*:not(:defined)`); daarvoor leg je ongestileerde tekst vast.

Er staat bewust **geen** pixelvergelijking met een baseline op. Zolang de omzetting loopt
verandert het beeld elke stap, en dan wordt een baseline elke stap opnieuw goedgekeurd.
Komt die er, dan hoort erbij: een gepinde `device_scale_factor`, vastleggen en vergelijken
in dezelfde container-image, en een drempel in plaats van een exacte match.

## De meetlat: gedrag, niet uiterlijk

Een omzetting mag een pagina er anders uit laten zien. Wat hij niet mag, is hem minder
laten doen. Dat gaat mis zonder dat iemand het merkt: een verdwenen keuzelijst, een knop
die iets anders aanroept, een invoerveld dat wegvalt - de pagina rendert gewoon, er komt
geen foutmelding, en je ontdekt het pas als je het nodig hebt.

In deze omzetting is dat meermalen gebeurd, en het is er niet uitgekomen door goed kijken
maar door meten. `scripts/lotc_compare_behaviour.py` haalt elke omgezette route twee keer
op - `?layout=roos` en `?layout=nldd` - en legt het gedragsoppervlak naast elkaar:

- waar je heen kunt: `href`, elk attribuut op `-href`, `action`
- wat htmx ophaalt: elke `hx-get`/`hx-post`/...
- welke JavaScript wordt aangeroepen: uit `onclick`, `@click`, `onchange`, `oninput`
- elk invoerveld met een naam, en elk `id` waar code aan kan hangen

Tagnamen, klassen, teksten en stylesheets tellen niet mee: dat IS de vormgeving.

```bash
uv run python scripts/lotc_compare_behaviour.py \
  --base https://zad.sandbox.rijksapp.dev \
  --secret "<SECRET_KEY van de draaiende applicatie>" \
  --email <adres dat toegang heeft> --project <projectnaam>
```

`tests/e2e/test_lotc_parity.py` is dezelfde meting als poort, tegen de testserver. De lijst
aanvaarde verschillen staat in het SCRIPT en wordt door de test geimporteerd - twee kopieen
zouden uit de pas lopen, en dan zegt de een schoon waar de ander kapot zegt.

Twee dingen om te weten voor je die poort vertrouwt:

- Hij meet wat er in de HTML staat, en dus alleen gedrag dat met de GEGEVENS van de
  testserver zichtbaar is. Die heeft geen Prometheus en geen ArgoCD. Voor die blokken is
  het script tegen een echte sandbox de meting, niet de test.
- Elk aanvaard verschil draagt een reden, en er is een test die dat afdwingt. Een
  uitzondering zonder reden is geen besluit maar een schuld.

De meetlat heeft zichzelf twee keer op vals alarm betrapt: hij keek alleen naar `href`
terwijl NLDD op sommige componenten `website-href` schrijft, en hij volgde de blokken niet
die een pagina bewust nalaadt, waardoor hij de dashboardmeters als verdwenen meldde. Dat
soort fout is erger dan geen meting - je leert hem negeren.

## Wat de omzetter met klikken deed

`@click="f()"` werd half gelezen: de attribuutregex zag alleen `click="f()"`, kende dat
attribuut niet op het component, en liet het vallen. Wat overbleef was een kale `@` in de
tag. **58 keer, in 35 bestanden** - knoppen die keurig renderen en zwijgen.

De omzetter maakt er nu een echte `onclick` van via LOTC's `:attrs`-spread, met de aanroep
in een `{% set %}`-blok vlak voor de tag. Dat blok is er om twee redenen: een genest
aanhalingsteken binnen `:attrs` leest de voorbewerker als het einde van het attribuut, en
de blokvorm rendert de Jinja die in zo'n aanroep zit gewoon mee.

## Blokken die diensten zelf leveren

Diensten leveren hun eigen sjablonen (`opi/services/catalog/<dienst>/`). Die zijn in
roos-componenten geschreven en renderen niet in de LOTC-omgeving: de map staat niet op dat
zoekpad, en twee componentsystemen kunnen sowieso niet in een Jinja-omgeving samen.

Er waren drie mogelijkheden, en twee ervan zijn fout. Weglaten laat functionaliteit
ongemerkt verdwijnen. In de andere omgeving renderen en de HTML inplakken (`render_roos()`)
leverde een blok op dat rvo-klassen draagt, en die worden op een LOTC-pagina door niets
opgemaakt: `lotc_rvo` staat niet in `DESIGN_SYSTEMS`. Het resultaat was dus niet "zichtbaar
onaf" maar kale, ongestileerde HTML - een derde uitkomst die niemand koos.

Blijft over: elke dienst schrijft zijn eigen LOTC-sjabloon, naast het roos-sjabloon en in
dezelfde map. Het bezwaar daartegen is echt - een tweede kopie loopt uit de pas zodra een
dienst zijn sjabloon wijzigt - en het antwoord daarop is
`tests/test_lotc_dienstblokken.py`: die toetst per sjabloon dat de tegenhanger BESTAAT en
dat hij hetzelfde DOET (dezelfde bestemmingen, htmx-adressen, JavaScript-aanroepen en
id's). De catalogusmap staat sinds RC-64 op het zoekpad van beide omgevingen.

Sinds RC-65 is `render_roos()` weg en heeft elk sjabloon in de catalogus zijn tegenhanger.

### En de fragmenten die zo'n blok NALAADT

Een omgezet blok is niet af zolang wat het met htmx ophaalt nog uit de oude omgeving komt:
het blok staat er in de nieuwe vormgeving, en na een scroll of een klik komt er een tabel
in de oude in. Twee daarvan waren zo:

- **De snapshotlijst** (`GET /projects/details/<project>/backups`). De route rendert nu via
  `render()` uit `opi/web/lotc_switch.py`, met `bg/_backup-snapshots.html.j2` +
  `bg/_backup-snapshots-one.html.j2` als tegenhanger. De id's met `hx-swap-oob` zijn
  letterlijk gelijk gebleven - het verzoek staat op `hx-swap="none"`, dus alles zonder die
  markering wordt weggegooid, en een stylesheet of script kan er daarom niet omheen.
- **De metingen per deployment** (`GET /projects/details/<project>/metrics/<deployment>`).
  Hier stonden voortgangsbalken met alleen de huidige waarde; het VERLOOP over de tijd was
  eruit. Terug naar dezelfde canvassen met dezelfde id's, en de tekencode staat sinds deze
  omzetting in `static/js/metrics_charts.js` - dezelfde verhuizing als
  `dashboard_gauges.js`, zodat beide vormgevingen er een kopie van gebruiken. Het fragment
  haalt Chart.js, de annotatie-plugin, die tekencode en zijn maten
  (`static/css/metrics-charts.css`) zelf op, precies een keer per document: de hertekende
  projectpagina laadt ze niet, want die weet niet dat dit blok bestaat.

Beide zijn gemeten in `tests/test_lotc_fragmenten.py` (gedragsoppervlak, sjabloon tegen
sjabloon) en in `tests/e2e/test_lotc_pariteit.py` (pixels op de canvassen, en de
herstelknop echt geklikt).

## Testen in twee vormgevingen

De bestaande e2e-tests zijn op de roos-markup geschreven. Toen nldd de standaard werd,
landden ze op de nieuwe pagina en faalden ze - niet omdat de applicatie stuk was, maar
omdat ze iets anders maten dan ze dachten.

`tests/e2e/conftest.py` zet daarom een `zad_layout=roos`-cookie in de
browsercontext. Dat is geen doofpot: `?layout=` in de URL wint van de cookie, en de tests
die de NIEUWE weergave meten zetten dat er zelf bij (`test_lotc_parity.py`,
`test_lotc_confirmations.py`, `test_lotc_project_tab.py`, `test_lotc_deployments_tab.py`).
Zo blijft het vangnet onder de release liggen en wordt de nieuwe vormgeving ook echt
getoetst, in plaats van dat een van de twee stilletjes onbewaakt raakt.

## Stand en wat er open staat

Alle templates compileren. De nieuwe vormgeving is de standaard, en op de sandbox staat de
meting op **nul verdwenen gedrag** over alle omgezette routes - met echte projecten, dus
inclusief ArgoCD en Prometheus.

Om: dashboard, projecten, projectdetails (vier tabbladen), diensten, about, voortgang,
uitnodigingen, metrics-explorer, de beheerpagina's en de wizard, plus de gedeelde dialoog
en de logviewer.

| open punt | bij wie |
|---|---|
| `architecture` - 1509 regels in een blok; verdient een eigen besluit, en staat op verzoek als laatste | ons |
| De blokken die diensten leveren dragen nog de oude opmaak (zie hierboven) | ons, per dienst |
| Het percentage in de dashboardmeter vraagt een RVO-kleurvariabele die NLDD niet heeft; erft nu de tekstkleur | ons |
| Iconen: de NLDD-woordenschat telt er 60, de RVO-set die roos meelevert 1163. Voorstel om die als losse implementatiemodule mee te nemen ligt bij LOTC | LOTC |

### Een aandachtspunt voor de bouw

LOTC komt van `git.claude.robbertuittenbroek.nl`. Dat werkt hier, maar een image die in
een andere omgeving gebouwd wordt moet die host kunnen bereiken. `jinja-roos-components`
komt van GitHub, dus een git-dependency is niet nieuw; deze host is dat wel.
