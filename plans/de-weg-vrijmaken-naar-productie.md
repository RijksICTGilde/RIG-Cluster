# De weg vrijmaken naar productie

Status: plan, 12 augustus 2026. Aanleiding: de generale repetitie (`docs/generale-repetitie-2026-08-12.md`) concludeert dat de keten gezond is maar dat er drie dingen open staan. Dit plan ruimt die op. Meer niet: alles wat die doorloop al heeft aangetoond hoeft niet opnieuw.

Lees dat verslag eerst. De bevindingnummers hieronder verwijzen ernaar.

## 1. Elk aanvinkvakje levert twee elementen met dezelfde `id` (bevinding 2)

### Wat er is, gemeten

Voor één aanvinkvakje staan er twee elementen in de lichte boom met dezelfde `id`:

```
DIV.lotc-checkbox-field   id=_services-config/keycloak/config/restrict-access/enabled
NLDD-CHECKBOX-FIELD       id=_services-config/keycloak/config/restrict-access/enabled
```

Het komt van twee kanten. De NLDD-tak van het LOTC-component `checkbox-field` zet de `id` op een omhullende `div` en leidt daar `-help` en `-error` van af, en geeft de attribuutbundel daarnaast door aan het binnenste `<nldd-checkbox-field>`. Ons `widgets/checkbox.html.j2` zet de `id` in diezelfde bundel (`:attrs="dict(field_attrs(field), id=field.path)"`), juist omdat de `id` op het **besturingselement** moet landen, want dat draagt `.checked`.

Onder het oude RVO-thema landden beide op hetzelfde `<input>` en viel het niet op. Onder NLDD zijn het twee elementen. Gevolg: ongeldige HTML, een risico voor `aria-describedby` en voor elke `label for=`, en `[id='<pad>']` levert er twee op in plaats van een.

Dit raakt **elk** enkel aanvinkvakje in de applicatie.

### Wat er moet gebeuren

De echte oplossing zit in het LOTC-component, en dat is een besluit en geen tikfout: de `id` weghalen bij het component kost de afgeleide `-help`/`-error`-id's, en weghalen uit de bundel kost de vindbaarheid van het besturingselement.

**Leg het daarom eerst voor aan het LOTC-project** (sessie `dclaude-lord-of-the-components-pr1`), met de meting hierboven en beide kanten van de afweging. Dat is deze week de werkende weg gebleken voor precies dit soort gevallen.

Kan of wil dat niet op tijd, kies dan aan onze kant de minst schadelijke van de twee en leg de reden vast. Het criterium is duidelijk: **een document met dubbele `id`'s is niet acceptabel**, dus doorschuiven zonder besluit is geen optie.

De toets is dat `document.querySelectorAll("[id='<pad>']")` er precies één oplevert, en dat de twee tests in `test_aanvinkvakje.py` weer groen zijn omdat het probleem weg is en niet omdat de test is aangepast.

## 2. Een taak met een mislukte subtaak meldt `completed` (bevinding 5)

### Wat er is, gemeten

Taak `0d504be5`, op een afgewezen dienstselectie:

```
"status": "completed", "progress_percent": 100,
"subtasks": [ ... {"name": "Component toevoegen", "status": "failed", ...} ],
"result": {"status": "failed", "error_type": "invalid_services"},
"error_message": "Services that must be enabled at project level first: ..."
```

De taak zegt dus `completed` terwijl zijn eigen `result` `failed` zegt en er een `error_message` staat. Een client die op `status` stuurt, en dat is de voor de hand liggende manier, concludeert dat het goed ging.

Dat is precies de klasse fout waar de zad-cli ons deze week meermaals op wees: een antwoord dat iets anders belooft dan er gebeurde. Het weigeren zelf is goed en gewenst; het rapporteren niet.

### Wat er moet gebeuren

Een taak waarvan een subtaak faalt, of waarvan het resultaat `failed` draagt, hoort dat in zijn eigen `status` te tonen. Kies bewust hoe: een subtaak die faalt terwijl de rest doorliep is niet hetzelfde als een taak die er helemaal niet gekomen is, en als dat onderscheid betekenis heeft, maak het dan zichtbaar in plaats van beide op `completed` te zetten.

Let op wat er van de huidige waarde afhangt: `progress_percent: 100` en de weergave van afgeronde taken in het portaal. Een status erbij die de UI niet kent, geeft een lege plek.

De toets: dezelfde afgewezen dienstselectie levert een taak op waarvan de status zegt dat het misging, en een geslaagde taak verandert niet van gedrag.

## 3. De wankele wizard-fixture in de sandbox-suite (bevinding 7)

25 tests in de sandbox-suite komen niet aan hun eigen toets toe. Het opruimen na een mislukking is in de repetitie al gerepareerd (`58629df7`); wat blijft is dat de fixture zelf niet betrouwbaar door de wizard komt.

Zolang dat zo is, is die suite geen bruikbare poort: hij zegt niets over de code, alleen over zichzelf. Maak hem betrouwbaar, of, als een deel niet te redden is, schrap dat deel met de reden erbij. Een rode test die iedereen negeert is erger dan geen test.

## De vier achterhaalde tests (bevindingen 3 en 4)

`test_lotc_project_tab.py` (drie tests) en `test_gedragsoppervlak` eisen een kopieerknop en een aanroep `copyToClipboard(...)` die er bewust niet meer zijn: dat is vervangen door `<c-secret-field ... show-copy />`, dat het klembord in het veld zelf heeft, en het vermogen is niet verloren.

Herschrijf ze naar wat de vangrail nu moet bewaken, of schrap ze met de reden. En ruim het spoor van de omzetting op: `bg/project-tabs.html.j2` zet nog `{% set kopieer = "copyToClipboard(...)" %}` zonder die variabele te gebruiken, en `project-details/section-config.html.j2` heeft hetzelfde patroon. Dat is dode code die mijn eigen wijziging heeft achtergelaten.

## Wat er NIET in deze taak zit

**De TLS-override per deployment-component (RC-78)** is in de repetitie niet getoetst en verdient dat wel voor de uitrol. Dat is een eigen doorloop en geen onderdeel hiervan; noem het in de afronding zodat het niet vergeten wordt.

**De paginamarge-test** (bevinding 1) blijft rood: die bewaakt een kolombreedte onder 1400 terwijl de gekozen bovengrens 1440 oplevert. Dat is een getal dat gekozen moet worden, geen fout, en die keuze ligt bij de eigenaar.

## De toets

- geen dubbele `id`'s meer: `[id='<pad>']` levert er precies één op, en `test_aanvinkvakje.py` is groen omdat de oorzaak weg is;
- een taak met een mislukte subtaak meldt dat in zijn `status`, en een geslaagde taak gedraagt zich als voorheen;
- de sandbox-suite komt aan zijn eigen toetsen toe, of wat dat niet kan is geschrapt met een reden;
- de vier achterhaalde tests zijn herschreven of geschrapt, en de dode `copyToClipboard`-sporen zijn weg;
- er staat vermeld dat RC-78 nog een eigen doorloop verdient.
