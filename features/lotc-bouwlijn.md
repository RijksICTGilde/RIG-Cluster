# LOTC-bouwlijn: OPI op Lord of the Components

De webinterface draait op **Lord of the Components** (LOTC) met het NLDD-thema. Sinds
RC-67 is dat het ENIGE componentensysteem: `jinja-roos-components` is weg, en daarmee de
schakelaar tussen de twee weergaven. Wat er precies uit is en wat de pariteitspoort
vervangt staat in `features/roos-eruit.md`.

Achtergrond en fasering: `plans/naar-het-nieuwe-componentensysteem.md` en
`plans/roos-eruit.md`. De samenleefmeting (historisch, uit de tijd dat beide systemen
naast elkaar stonden): `docs/lotc-samenleven-met-jinja-roos.md`.

## Wat het is

- **Elke route** rendert zijn pagina door LOTC: `/services`,
  `/dashboard`, `/projects` en `/projects/details/<naam>` (met het resourcegebruik-
  fragment dat htmx apart inlaadt), plus de beheerpagina's `/admin/users` (inclusief het
  formulier op `/admin/users/create` en `/admin/users/<id>/edit`), `/admin/approvals` en
  `/admin/usage`, en **de wizard** (`/forms/wizard/start`, `/forms/wizard/<flow>`,
  `/forms/wizard/<flow>/edit/<project>`, elke htmx-stap en de samenvatting).
- Daarnaast een **proefopstelling** onder `/lotc/`, met voorbeeldprojecten. Die is er om
  vorm te kiezen zonder een cluster nodig te hebben, niet als eindbestemming.
- De navigatie volgt de opzet van [bg.rijks.app](https://bg.rijks.app/): hoofdnavigatie
  in een zijkolom met groepen, alleen hulplinks (account, in- en uitloggen) in de header.
- De formulierlaag is te bekijken op `/lotc/formulier`.

Er valt niets meer te kiezen: `opi/web/lotc_switch.py` rendert het sjabloon, en de
`?layout=`-parameter en het koekje `zad_layout` bestaan niet meer. Wat er wel nog gekozen
wordt is licht/donker, via `/weergave` en het koekje `zad_scheme`.

Lord of the Components is een gewone runtime-dependency: de applicatie rendert er
pagina's mee, dus hij hoort in de image.

## Gebruik

```bash
cd operations-manager/python
uv sync
uv run pytest tests/test_lotc_icon_mapping.py tests/test_lotc_layout_rules.py -q
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
| `opi/templates_lotc/` | alle templates. De eerste generatie is ooit gegenereerd; de omzetter is met zijn invoer verdwenen, dus dit is nu handwerk |
| `opi/templates_lotc/base_lotc.html.j2` | de schil in bg-opzet - **wel** met de hand |
| `opi/templates_lotc/widgets/` | de formulierwidgets - **wel** met de hand |
| `opi/templates_lotc/bg/` | de hertekende pagina's - **wel** met de hand |
| `opi/templates_lotc/bg/_patterns.html.j2` | gedeelde patronen: `panel()`, `page_head()`, `info()`, `service_card()` |
| `opi/templates_lotc/bg/wizard-page.html.j2`, `wizard-start.html.j2`, `_wizard-step.html.j2`, `_wizard-steps.html.j2`, `_wizard-review.html.j2` | de wizard: de hele pagina, de startpagina, en de drie fragmenten die htmx wisselt |
| `opi/web/lotc_switch.py` | `render()`/`render_fragment()`, plus de vorm waarin een route zijn gegevens aanlevert |
| `opi/web/lotc_fixtures/` | voorbeeldprojecten voor de proefopstelling |
| `opi/web/navigation_lotc.py` | de indeling van de navigatie en de icoonvertaling |
| `opi/web/lotc_router.py` | de routes onder `/lotc/` |
| `opi/forms/widgets/lotc.py` | de widget-adapter die de LOTC-templates rendert |
| `opi/forms/widgets/fields.py` | de gedeelde veldvoorbereiding waar die adapter van erft |
| `opi/forms/lotc_attrs.py` | de attribuutbundel van een veld, voor LOTC's `:attrs`, plus de foutbedrading van een veld |
| `opi/templates_lotc/components/_forms.j2` | onze kopie van de veldmacro's van `lotc-forms`; zie "Een sjabloon van een design system overschrijven" |

### Waarom er ooit twee omgevingen waren

Beide componentsystemen registreren een Jinja-extensie die de bron voorbewerkt en **elke**
`<c-*>`-tag opeist. In één `Environment` claimt de eerst geregistreerde voorbewerker alles
en breekt op de tags die hij niet kent; een doorlaatstand bestaat aan geen van beide
kanten. Twee losse omgevingen werkten wel, met de grens per overervingsketen. Dat is
gemeten in `docs/lotc-samenleven-met-jinja-roos.md` en het is de reden dat de omzetting
per pagina kon. Er is er nu nog één.

### De omzetter is weg

De eerste generatie van `opi/templates_lotc/` is gegenereerd uit `opi/templates/`: een met
de hand overgetypte kopie van 152 bestanden is vanaf dag twee verouderd zonder dat iemand
ziet waar. Die invoer bestaat niet meer, dus de omzetter ook niet.

Let op wat daarvan is blijven liggen: naast de handgeschreven `bg/`-pagina's staat nog de
eerste automatische omzetting van de oude boom. Die hangt aan `/lotc/pagina/<naam>` en aan
een paar tests, en is NIET wat een gebruiker ziet.

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

### Een sjabloon van een design system overschrijven

`setup_components` APPENDT de sjabloonpaden van de design systems achter onze eigen
`templates_lotc/`. Een bestand op dezelfde naam wint dus van dat van het pakket. Dat is de
manier om een bug in het thema te overbruggen zonder hem op tien plekken na te bouwen.

Er staat er nu één: `opi/templates_lotc/components/_forms.j2`, onze kopie van de gedeelde
macro's van `lotc-forms`. Elk veldsjabloon importeert die, dus het is één plek voor alle
veldsoorten. Twee wijzigingen zitten erin, allebei met een verzoek in
`request_for_components.md`:

1. **De foutmelding wordt bedraad.** `nldd-form-field` toont alleen foutregels waarvan het
   id in `error-message` OP HET INVOERVELD staat; `lotc-forms` schrijft daar
   `error-message-ids`, en dat is de andere richting (die eigenschap zet `nldd-form-field`
   zelf om `aria-describedby` te bedraden). Zonder de bedrading staat de melding er wel en
   is hij `display: none` met hoogte 0. `bedraad_foutmelding` in `opi/forms/lotc_attrs.py`
   zet `invalid`, `aria-invalid` en `error-message` op de besturing; dat laatste is bij de
   groepsvelden (radio, aankruisvakjes) het enige dat een schermlezer over de fout krijgt.
2. **`data-no-optional-badge` laat "Optioneel" weg.** `lotc-forms` zet dat label op elk
   veld dat niet `required` is (rijksconventie: markeer optioneel, niet verplicht). Voor
   een kiezer met een vaste selectie of het enige veld van een herhaalbaar item betekent
   het niets. Zet dan dit merk-attribuut op de besturing en géén `required`: dat haalt het
   label ook weg, maar laat de HTML beweren dat er iets ingevuld moet worden, en
   formuliervalidatie leest dat ook echt.

Een kopie is een schuld: hij mist een verbetering van bovenstrooms in stilte. Daarom legt
`tests/test_lotc_foutmelding_veld.py` hem naast de geïnstalleerde versie (modulo de
bewuste regels), toetst hij dat onze kopie ook echt wint op de searchpath, en toetst hij
dat het origineel de bug nog heeft - is die weg, dan kan de kopie weg.

## Testen

| test | bewaakt |
|---|---|
| `tests/test_lotc_component_names.py` | dat er nergens nog een `<c-p>` staat; die naam bestaat niet |
| `tests/test_lotc_icon_mapping.py` | dat elke iconnaam een icoon OPLEVERT, gemeten tegen de geleverde NLDD-bundel |
| `tests/e2e/test_lotc_visual.py` | dat pagina's in een browser kloppen, met screenshots |
| `tests/test_lotc_layout_rules.py` | dat kaarten via `panel()` gebouwd worden en gaps uit de schaal komen |
| `tests/test_lotc_schrijfwijze.py` | dat teksten de lezer met "je" aanspreken |
| `tests/e2e/test_gedragsoppervlak.py` | dat een pagina of dialoog niets verliest van wat er vastligt |
| `tests/test_lotc_modal_fragmenten.py` | hetzelfde voor de dialoogfragmenten die zonder takendienst niet via HTTP te bereiken zijn |
| `tests/test_lotc_foutmelding_veld.py` | dat de veldfout bedraad wordt, en dat onze kopie van `components/_forms.j2` alleen op de bedoelde punten van de geïnstalleerde afwijkt |
| `tests/e2e/test_lotc_veldfout_zichtbaar.py` | dat die foutmelding in een browser HOOGTE heeft - "staat de tekst er" was jarenlang groen terwijl niemand hem zag |
| `tests/test_lotc_optioneel_badge.py` / `tests/e2e/test_lotc_optioneel_label.py` | dat "Optioneel" weg is waar het niets betekent, zonder het veld verplicht te noemen |

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
maar door meten. De meetlat staat in `tests/oppervlak.py`:

- waar je heen kunt: `href`, elk attribuut op `-href`, `action`
- wat htmx ophaalt: elke `hx-get`/`hx-post`/...
- welke JavaScript wordt aangeroepen: uit `onclick`, `@click`, `onchange`, `oninput`
- elk invoerveld met een naam, en elk `id` waar code aan kan hangen

Tagnamen, klassen, teksten en stylesheets tellen niet mee: dat IS de vormgeving.

Zolang beide vormgevingen bestonden werd die meting TWEE keer gedaan - `?layout=roos`
tegen `?layout=nldd` - en was de oude pagina de norm. Sinds RC-67 is de norm een
vastgelegde lijst; zie `features/roos-eruit.md` voor hoe je die bijwerkt en waarom je die
diff moet lezen.

Twee dingen om te weten voor je die poort vertrouwt:

- Hij meet wat er in de HTML staat, en dus alleen gedrag dat met de GEGEVENS van de
  testserver zichtbaar is. Die heeft geen Prometheus en geen ArgoCD.
- Hij faalt alleen op wat WEG is. Iets erbij is nieuw werk.

De meetlat heeft zichzelf twee keer op vals alarm betrapt: hij keek alleen naar `href`
terwijl NLDD op sommige componenten `website-href` schrijft, en hij volgde de blokken niet
die een pagina bewust nalaadt, waardoor hij de dashboardmeters als verdwenen meldde. Dat
soort fout is erger dan geen meting - je leert hem negeren.

## Wat de omzetter met klikken deed

`@click="f()"` werd half gelezen: de attribuutregex zag alleen `click="f()"`, kende dat
attribuut niet op het component, en liet het vallen. Wat overbleef was een kale `@` in de
tag. **58 keer, in 35 bestanden** - knoppen die keurig renderen en zwijgen.

De omzetter maakt er nu een echte `onclick` van via LOTC's `:attrs`-spread, met de aanroep
in een `{% set %}`-blok vlak voor de tag. Dat is meteen het antwoord op "hier hoort een
kale `<button>` want die heeft een onclick": nee, dat kan gewoon op een `<c-button>`.
Welke maat en welk `type` een knop draagt staat in `features/knopmaten.md`. Dat blok is er om twee redenen: een genest
aanhalingsteken binnen `:attrs` leest de voorbewerker als het einde van het attribuut, en
de blokvorm rendert de Jinja die in zo'n aanroep zit gewoon mee.

## Blokken die diensten zelf leveren

Diensten leveren hun eigen sjablonen (`opi/services/catalog/<dienst>/`), en de projectpagina
rendert die zonder te weten welke dienst het is. De catalogusmap staat sinds RC-64 op het
zoekpad van de omgeving.

Zolang beide systemen bestonden lag naast elk roos-sjabloon een `-lotc`-tegenhanger, en
zocht `lotc_counterpart()` die op. Daarvoor was er `render_roos()`, dat een blok in de
andere omgeving rendeerde en de HTML inplakte - wat een blok opleverde met rvo-klassen die
op een LOTC-pagina door niets opgemaakt worden (`lotc_rvo` staat niet in `DESIGN_SYSTEMS`),
dus niet "zichtbaar onaf" maar kaal.

Sinds RC-67 heeft elke dienst nog EEN sjabloon, onder zijn eigen naam.
`tests/test_lotc_dienstblokken.py` toetst dat elk sjabloon in de catalogus rendert, dat er
geen markup van het oude systeem uit komt, en dat elke dialoog zijn gegevens kan
wegsturen.

### En de fragmenten die zo'n blok NALAADT

Een omgezet blok is niet af zolang wat het met htmx ophaalt nog uit de oude omgeving komt:
het blok staat er in de nieuwe vormgeving, en na een scroll of een klik komt er een tabel
in de oude in. Twee daarvan waren zo:

- **De snapshotlijst** (`GET /projects/details/<project>/backups`). De route rendert nu via
  `render()` uit `opi/web/lotc_switch.py`, met `shared/_backup-snapshots.html.j2` +
  `shared/_backup-snapshots-one.html.j2`. De id's met `hx-swap-oob` zijn
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

Beide zijn gemeten in `tests/test_lotc_fragmenten.py` (welke canvassen er horen te staan,
waar de tijdvakknoppen op mikken, welke id's buiten de band binnenkomen) en in
`tests/e2e/test_lotc_pariteit.py` (pixels op de canvassen, en de herstelknop echt
geklikt).

## Iconen: meet de BUNDEL, niet de lijst

`icons.json` van `lord_of_the_components` noemt 327 namen; de `nldd.js` die de browser
laadt bevat er 271. De 56 namen ertussen bestaan op papier en renderen als niets. Dat is
geen randgeval: het kostte 37 lege plekken in de interface, waaronder de bewerkknop en de
verwijderknop, terwijl de test die erop bewaakte groen stond - want die las de lijst.

De bron is nu `opi/web/nldd_iconen.py`, dat de namen uit de geleverde bestanden haalt.
En let op het tweede gat dat daarbij hoorde: `ROOS_TO_NLDD_ICONS` wordt toegepast door het
`nldd_icon`-FILTER. Een letterlijke `icon="verwijderen"` in een sjabloon komt daar nooit
langs. In een sjabloon schrijf je dus een NLDD-naam; de tabel is voor namen die uit
Python komen (het menu, de dienstdefinities). Zie
`features/eigen-vormgeving-vervangen-door-componenten.md`.

## Stand en wat er open staat

Alle templates compileren, en de omzetting is af: er is geen tweede vormgeving meer om op
terug te vallen.

Om: dashboard, projecten, projectdetails (vier tabbladen), diensten, about, voortgang,
uitnodigingen, metrics-explorer, de beheerpagina's en de wizard, plus de gedeelde dialoog
en de logviewer.

| open punt | bij wie |
|---|---|
| `architecture` - 1509 regels in een blok; verdient een eigen besluit, en staat op verzoek als laatste | ons |
| Het percentage in de dashboardmeter vraagt een RVO-kleurvariabele die NLDD niet heeft; erft nu de tekstkleur | ons |
| Iconen: de NLDD-bundel levert er 271, de RVO-set die roos meeleverde 1163. Voorstel om die als losse implementatiemodule mee te nemen ligt bij LOTC | LOTC |
| De open verzoeken aan het thema staan sinds RC-70 gebundeld in `request_for_components.md` | LOTC |

### Een aandachtspunt voor de bouw

LOTC komt van `git.claude.robbertuittenbroek.nl`. Dat werkt hier, maar een image die in
een andere omgeving gebouwd wordt moet die host kunnen bereiken. Dat was al zo toen
`jinja-roos-components` er nog naast stond; sinds die weg is, is het de enige
git-dependency van de interface.
