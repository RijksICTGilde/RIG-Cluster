# Eigen vormgeving vervangen door componenten

Wat het thema levert, gebruiken we. Wat we met de hand nabouwden, gaat eruit. Deze
wijziging (RC-70) begint bij het logpaneel - het duidelijkste geval - en pakt daarna de
twee dingen die daaruit boven kwamen: iconen die als een lege plek renderen, en knoppen
die los boven hun onderwerp zweven.

Het doel is nadrukkelijk NIET nul eigen CSS. Het doel is dat er geen component wordt
nagebouwd. Waar het thema niets levert, blijft eigen opmaak staan - met erbij waarom.

## Het logpaneel is een `<c-sheet>`

Het paneel met de live logstroom was een zijpaneel dat met de hand was nagebouwd: een
waas over de pagina, een vast gepositioneerd vlak dat met een `transform` naar binnen
schoof, een kopbalk, vier vierkante knopjes, een zoekveldje, vier gekleurde badges met
een verborgen aanvinkvak eronder, en een schakelaar voor de regelterugloop. Alles in
vaste hexkleuren, dus zonder licht/donker en zonder themawissel.

| onderdeel | was | is |
|---|---|---|
| paneel, waas, schuifanimatie, Escape, klik ernaast | eigen CSS + eigen toetsafhandeling | `<c-sheet>` (een native `<dialog>`) |
| kopbalk met titel, ondertitel en sluitknop | eigen CSS | `<nldd-top-title-bar>` |
| pauzeren, leegmaken, kopieren, downloaden | vier `.log-control-btn` van 36x36 | `<nldd-toggle-button>` + `<nldd-icon-button>` in een `<nldd-toolbar>` |
| componentkeuze | kale `<select>` met eigen opmaak | dezelfde `<select>`, in een `<c-dropdown>` |
| zoeken | eigen veld, eigen wisknop | `<c-search-field>` |
| niveaufilters | verborgen aanvinkvakken met eigen badges | `<nldd-toggle-button-group>` |
| regelterugloop | verborgen aanvinkvak met eigen label | `<c-switch-field>` |
| lege toestand | eigen CSS | `<c-inline-dialog>` |

Netto: 176 regels eigen CSS eruit, en het paneel volgt nu de licht/donker-keuze van
`/weergave`.

### Eerst meten of het component het gedrag verdraagt

Dit paneel is het enige venster in de applicatie dat zichzelf vult TERWIJL het openstaat:
er hangt een WebSocket aan en de regels komen binnendruppelen. Een dialoog die zijn
inhoud bij het OPENEN ophaalt, zou dat niet kunnen. Die vraag is beantwoord voordat er
iets omging, op twee manieren:

1. **In de bron van `nldd.js`.** `<nldd-sheet>` rendert een native `<dialog>` met een kale
   `<slot>` erin. De inhoud blijft in de light DOM van het document staan en wordt alleen
   doorgegeven; `show()` en `hide()` openen en sluiten de dialog en gooien niets weg. Er
   is geen moment waarop het component de inhoud vervangt of opnieuw opbouwt.
2. **In een browser.** `tests/e2e/test_logviewer_gedrag.py` vervangt de WebSocket door een
   nepexemplaar en duwt er regels doorheen. Die test stond er VOOR de omzetting, was toen
   groen, en is er ongewijzigd doorheen gekomen.

### Wat de omzetting aan gedrag verandert

- **Sluiten loopt via het `close`-event van de sheet.** Er zijn nu vier manieren om het
  paneel dicht te doen (de knop Sluiten, Escape, een klik ernaast, `closeLogViewer()`) en
  er is er maar een die de WebSocket mag opruimen. Hing die opruiming aan de knop, dan
  liet Escape een pod achter die blijft streamen.
- **De WebSocket sluit aan het EIND van de schuifanimatie**, niet bij de klik. Een sheet
  meldt zich pas dicht als hij uit beeld is. Hij wordt opgeruimd, alleen een fractie later.
- **De eigen Escape-afhandeling is weg.** Escape wiste eerst de zoekopdracht en sloot het
  paneel pas daarna. Dat doen de componenten nu allebei zelf: de `<dialog>` sluit op
  Escape, en het zoekveld is intern een `<input type="search">`, waar de browser Escape
  op afhandelt door de inhoud te wissen zonder dat de dialog dichtgaat. Gemeten door de
  regels weg te halen en te kijken of een test het merkte - dat deed er geen.
- **`#log-viewer-backdrop`, `#log-viewer-deployment`, `#log-pause-icon` en
  `#log-search-clear` bestaan niet meer.** Wat ze deden, doet het thema. Dat de
  INFORMATIE er nog staat (de deploymentnaam, de gekozen component) is een assertie
  geworden in plaats van een id in een lijst.

### Wat er met opzet eigen blijft

- **De regelbak** (`.log-viewer-content`, `.log-line`). `<c-code-viewer>` toont EEN tekst
  die je in zijn geheel meegeeft; deze bak krijgt losse regels aangeschoven, verbergt een
  deel per niveau en markeert zoektreffers binnen een regel.
- **De statusregel met het bolletje.** Het thema heeft geen aanduiding voor "leeft de
  verbinding nog".
- **De bedieningsrij** is een kale flexrij en met opzet geen `<c-container>`: de sheet zet
  `::slotted(nldd-container)` op `flex-grow: 1`, dus zo'n container groeit net zo hard als
  de regelbak.

Alle vier staan als verzoek in `request_for_components.md`.

### Klassen die eruitzien als opmaak en het niet zijn

`config-item`, `config-code`, `copy-btn`, `deployment-section`, `is-hidden`,
`log-viewer-panel`, `search-highlight` en `word-wrap` hangen aan JavaScript. Ze zijn
gebleven.

## Iconen die als een lege plek renderen

Bij het bekijken van het omgezette paneel bleken twee van de vijf knoppen in de kop leeg.
Dat bleek geen incident.

**Gemeten in een browser** - elke iconnaam uit de sjablonen door een echte `<nldd-icon>`
en `<nldd-button>`, met de vraag of er een pad in het SVG zat: **37 van de 79 namen
renderden als niets**, waaronder de bewerkknop en de verwijderknop.

Er waren twee oorzaken, allebei stil:

1. **De poort las de verkeerde bron.** `tests/test_lotc_icon_mapping.py` toetste tegen
   `icons.json` van `lord_of_the_components` (327 namen). De bundel die de browser laadt
   bevat er 271. De 56 namen ertussen bestaan op papier. De test was jarenlang groen.
2. **De vertaaltabel wordt op sjablonen niet toegepast.** `ROOS_TO_NLDD_ICONS` loopt via
   het `nldd_icon`-FILTER. Een letterlijke `icon="verwijderen"` in een sjabloon komt daar
   nooit langs: de naam staat in de tabel, hij wordt niet vertaald, en hij rendert als
   niets. De poort liet zulke namen door omdat ze "in de tabel stonden".

### Wat er nu staat

- **`opi/web/nldd_iconen.py`** leest de namen uit de GELEVERDE bestanden - dezelfde plek
  waar de browser ze vandaan haalt - inclusief de vriendelijke namen die NLDD zelf
  doorverwijst (`search` -> `magnifier`). Niet overgeschreven, want een handgeschreven
  kopie veroudert stilzwijgend bij een versiebump.
- **De sjablonen dragen NLDD-namen.** 32 namen op 178 plekken omgezet.
- **De poort is hard.** `tests/test_lotc_icon_mapping.py` faalt op elke iconnaam in elk
  sjabloon (inclusief `opi/services/catalog/`) en op elk dienstpictogram dat na vertaling
  geen icoon oplevert. De uitzondering voor namen "die in de tabel staan" is weg, en
  `KNOWN_GAPS` is leeg.
- **`to_nldd_icon()` zwijgt niet meer.** Een naam die geen icoon oplevert, geeft een
  `logging.warning`. Doorlaten blijft juist - een verkeerd icoon tonen is erger dan een
  lege plek - maar niets zeggen was de slechtste van de drie.

Twee namen hadden geen letterlijke tegenhanger en hebben er nu een die hetzelfde zegt:
`uit-aanknop` -> `moon` (een slapende deployment) en `weegschaal` -> `score-meter`.

**Waar de fout valt, en waarom daar.** In een test, niet bij het renderen. Hard falen bij
het renderen is het duidelijkst, maar dan sloopt een typefout in een icoonnaam een hele
pagina - en dat is een zwaardere prijs dan het probleem. Er zijn 79 namen en ze staan
allemaal in sjablonen die je kunt scannen, dus een test vangt het voordat het uitgerold
wordt en breekt niets in productie. De logregel is het vangnet eronder, voor namen die
pas tijdens het draaien ontstaan.

## Knoppen die los boven hun onderwerp zweven

Twee plekken waar een blokkop-met-actie met de hand was neergezet in plaats van door een
component.

**De bewerkknop van een blok** (Team, Services & Integraties) stond als eerste kind IN de
inhoud: onder de uitleg, boven de tabel. Dat las als een knop die nergens bij hoort - geen
kop ernaast, geen kader eromheen. Hij staat nu in de kopregel, via de `aside`-parameter
van `panel()`, die hem aan `<c-section-head>` doorgeeft: titel links, actie rechts, als
een geheel.

Het kopicoon vervalt daarmee vanzelf (`panel()` laat het weg zodra er een actie staat).
Dat is winst: helemaal rechts in de kopregel zag dat icoon er zelf uit als nog een knop,
terwijl het alleen de titel herhaalde.

**De verwijderknop van een herhaalbaar item** (bijvoorbeeld een extra Keycloak-client)
zat in een eigen `<div class="lotc-sequence__item-header">` met een LEGE `<span>` ernaast,
puur om hem met `justify-content: space-between` naar rechts te duwen. Ook dat is nu
`<c-section-head>`, met een titel erbij.

Die titel is niet alleen vormgeving. Bij een reeks IN een reeks - extra Keycloak-clients,
elk met hun eigen redirect-URI's - stonden er twee verwijderknoppen onder elkaar met
verschillende betekenis, en niets zei welke welke was. Nu staat erbij WAT je verwijdert.

De titel komt uit `field.attributes.get('item_label')` als die er is, en anders uit het
veldlabel met een volgnummer ("Extra Keycloak clients 1"). Dat laatste is niet mooi;
`item_label` is de weg om het per veld beter te maken.

## Testen

```bash
cd operations-manager/python
uv run pytest tests/test_lotc_icon_mapping.py -q
uv run pytest tests/e2e/test_logviewer_gedrag.py -m "e2e and not sandbox" -q
uv run pytest tests/e2e/test_gedragsoppervlak.py -m "e2e and not sandbox" -q
```

| test | bewaakt |
|---|---|
| `tests/e2e/test_logviewer_gedrag.py` | dat het paneel opent, regels binnenkrijgt terwijl het openstaat, filtert, pauzeert, van component wisselt, sluit, en de WebSocket opruimt |
| `tests/test_lotc_icon_mapping.py` | dat geen enkele iconnaam als een lege plek rendert, gemeten tegen de GELEVERDE bundel |
| `tests/e2e/test_gedragsoppervlak.py` | dat er niets van de vastgelegde lijst verdwijnt |

Het beeld staat in `tests/e2e/screenshots/lotc/logviewer-open.png` en is bedoeld om
bekeken te worden. Groene tests zeggen niets over hoe een scherm eruitziet: de twee lege
knoppen in de kop van dit paneel zijn op een screenshot gevonden, niet in een assertie.
