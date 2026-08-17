# De generale: de hele suite tegen een nieuwe versie van ZAD

De releasebranch `release-augustus-2026` heeft er sinds de vorige doorloop een stapel wijzigingen bij gekregen, en een deel daarvan raakt dingen die geen enkele test tot nu toe heeft gezien: een probeserver op een eigen poort en een eigen containerpoort in de deployment, formulierverzending die de browservalidatie omzeilt, en een gegenereerd stuk OpenAPI. Dit is de laatste meting voor de merge naar main.

**Alles moet groen.** Niet "groen op de bekende falers", niet "die twee doen het altijd al niet". Groen. En groen tegen een build van deze branch, niet tegen wat er toevallig nog draait.

## Vooraf

Het slot claimen met `orch sandbox claim`; deze doorloop houdt het cluster uren bezet.

De drie controles die de vorige rondes geld gekost hebben, in deze volgorde:

- `kubectl config current-context` is `kind-rig-sandbox`. Staat hij op productie, dan gaat een sync daarheen en lijkt alles stil te staan.
- `curl -sk https://zad.sandbox.rijksapp.dev/version` geeft dezelfde commit als `git rev-parse --short HEAD`, en **vijf keer achter elkaar**. Vlak na een deploy antwoorden de oude en de nieuwe pod allebei, en dan meet je een mengsel.
- `uv sync --all-groups` in de worktree, anders faalt de pre-push hook op een ontbrekende module.

En de valkuil die RC-110 een rode suite kostte, `E2E_SECRET_KEY` haal je zo op en niet met een jsonpath op een sleutel die niet bestaat (dat geeft geen fout maar een lege string, en dan faalt alles op de inlogpagina):

```bash
kubectl -n rig-system get cm operations-manager-config -o jsonpath='{.data.\.env}' \
  | grep -E '^SECRET_KEY=' | cut -d= -f2-
```

## Taken

### 1. Een verse sandbox met een verse build

`task sandbox:destroy` en daarna `task sandbox:setup`, vanaf nul. De helft van wat de vorige rondes zochten kwam voort uit toestand die er nog stond.

Daarna de versie onder toets uitrollen en de `/version`-controle hierboven doen.

Verifieer bij het opkomen twee dingen die nieuw zijn in deze release:

- De pod heeft een tweede containerpoort `probe` (8001) en de drie probes wijzen daarop. `kubectl -n rig-system describe pod` moet dat tonen, en `kubectl -n rig-system port-forward` naar 8001 moet op `/healthz` antwoorden.
- De pod wordt `Ready` zonder herstarts. Een probe op een verkeerde poort geeft een CrashLoop die er als iets anders uitziet.

### 2. Unit, e2e en sandbox

- `uv run pytest tests/ -q` met de eigen standaardaanroep van het project. Geef **geen** eigen `-m` mee, dan zet je infrastructuurtests aan die hier niet kunnen draaien.
- `uv run pytest -m e2e -q`. Draait tegen een eigen server, heeft de sandbox niet nodig.
- `uv run pytest -m sandbox -q` tegen het draaiende cluster.

Verifieer per suite: nul failures, nul errors. Een test die overslaat is goed, een test die faalt niet.

Faalt er een, dan **eerst uitzoeken of het aan de test of aan de code ligt** en dat opschrijven. Bij RC-108 zaten veertien van de vijftien bevindingen in de testlaag; dat is een reden om beter te kijken, niet om sneller te concluderen.

### 3. De reallife-suite

`uv run pytest -m reallife -q`, en daarnaast `uv run pytest -m punt14 -q`. Reken op ruim een uur voor het paar.

Laat ze **gelijktijdig** draaien, net als in RC-112: de punt-14-jacht meet alleen iets als er werkelijk belasting op de ProjectStore staat.

Punt 14 is in RC-112 in 92 pogingen niet gereproduceerd. Dat blijft het vertrekpunt; deze taak hoeft er niet opnieuw op te jagen, maar meldt het wel als het zich alsnog voordoet.

### 4. Wat deze release nieuw heeft, in de browser

De geautomatiseerde suites raken dit maar half. Loop dit met de hand na, met `scripts/kijk_sandbox.py` erbij, en **kijk ook echt naar de plaatjes**.

1. **De wizard slikt geen verzendingen meer.** In de bijlagenstap: een bijlage toevoegen en verwijderen. In de cross-domain-stap, zowel in de create-wizard als in de modal-edit: een bronproject kiezen en controleren dat de deployment- en componentlijst daadwerkelijk opnieuw geladen worden. Dit ging fout doordat de browser de verzending stil blokkeerde op een leeg verplicht veld; de knoppen deden dus niets zonder enige melding.
2. **Aliassen als een blok.** Een component bewerken en opslaan, en daarna in de projectenrepository controleren dat de aliassen als een AGE-blok zijn weggeschreven en niet per sleutel. Op het scherm mag geen versleutelde tekst staan en geen redactiemarkering in het bewerkveld.
3. **Het dashboard.** De verdeling per project toont geheugen en CPU, gesorteerd op geheugen, en de projectnamen zijn klikbaar.
4. **Een bijlage vervangen** via de UI, met behoud van de id.
5. **Een backup terugzetten** en daarna gewoon verder kunnen werken; na een restore stond het project op slot (RC-111).
6. **De schermen die van naam zijn veranderd**: Mijn projecten, Services overzicht, de kop Services overzicht, en het zoek- en sorteergebied op de projectenpagina dat als een geheel ververst met behoud van focus in het zoekveld.
7. **De CLI- en Actions-pagina**: de repositorylink staat bovenaan, en de Actions-pagina laat zien hoe je meerdere images in een keer bijwerkt.

Per punt: werkt het, of werkt het niet en wat stond er dan.

### 5. De API-weg en de documentatie

Dezelfde doorloop met `curl` tegen `/api/v2`: project aanmaken, dienst toevoegen, component toevoegen, uitrollen, opvragen, verwijderen. De CLI leunt hierop en heeft er dit jaar dertien bevindingen op gemeld.

Nieuw en dus expliciet te toetsen: **de toegestane waarden staan in `/openapi.json`**.

- Haal `/openapi.json` van het draaiende cluster op en controleer dat een veld met een vaste keuzelijst, bijvoorbeeld de slaapstand, daar een `enum` heeft en niet alleen een zin in de beschrijving.
- Een veld waarvan de keuzes per project verschillen hoort geen verzonnen `enum` te hebben, maar een machineleesbare verwijzing naar waar de keuzes vandaan komen.
- Wijkt een keuzelijst in het formulier af van wat het configuratiemodel toestaat, dan is **dat** een bevinding en geen documentatiekwestie.

### 6. Het verslag

Een document in `docs/`, met per taak wat er gedaan is, wat er gemeten is en wat er misging. **Ook wat er misging.** Een doorloop die alleen successen opsomt is geen doorloop.

Sluit af met een oordeel in een zin: deze branch kan naar main, of deze branch kan niet naar main en dit is waarom.

## Wat er buiten valt

- Repareren. Een bevinding wordt een eigen taak met een eigen test, tenzij hij de merge blokkeert; dan staat dat er met zoveel woorden bij.
- Productie. Deze doorloop gaat alleen over de sandbox.
- Nieuwe functionaliteit. Wat hier gevonden wordt en niet blokkeert, wordt een eigen taak en geen sluipende uitbreiding van deze.
