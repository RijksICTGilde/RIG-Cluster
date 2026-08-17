# De reallife-suite draaien, met punt 14 als doel

De `reallife`-suite is deze hele cyclus niet gedraaid. Dat is de enige suite die vijf projecten met semi-gelijktijdige mutaties via UI én API tegelijk doet, en daarmee het enige dat lijkt op wat er in productie gebeurt. Alles wat we nu groen noemen is gemeten met één ding tegelijk.

Er is een tweede reden om hem nu te draaien, en die is concreter: **punt 14 van de zad-cli**.

## Punt 14, en waarom deze suite de beste kans is

`zad deployment create productie --component web` faalt af en toe:

```
error_type: deployment_not_found
failed:    Component aan deployment toevoegen: Deployment 'productie' not found in project 'p1-huk'
completed: Component validatie
```

Dezelfde taak maakt de deployment aan en vindt hem in zijn volgende deelstap niet terug.

Wat de zad-cli al heeft uitgesloten: een tweede doorloop direct erna kwam volledig door (44 van de 44 stappen), en los naspelen lukte in geen van de drie vormen die zij probeerden (met uitrollen aan, met uitrollen uit terwijl er al een deployment is, en de eerste deployment van een vers project met uitrollen uit). Het treedt alleen op als er **genoeg niet-uitgerolde wijzigingen wachten**.

Dat is een toestand die je met de hand nauwelijks maakt en die deze suite juist wel maakt. Vandaar dat het één taak is en geen twee.

Wat ik al heb uitgesloten, zodat niemand het opnieuw doet: `ProjectStore.read_path` bedient een HEAD-lezing uit de in-memory cache en niet uit git, en `save` heeft een `refresh_cache`-schakelaar — maar **geen enkele aanroeper zet die uit**. Die verklaring is dus dood. Het mechanisme is nog niet aangewezen.

## Vooraf

Het sandboxcluster wordt opnieuw uitgerold. **Wacht tot dat klaar is** en claim daarna het slot met `orch sandbox claim`, want deze suite houdt het cluster een uur bezet en haalt anders andermans werk onderuit.

Controleer als eerste, net als in de vorige doorlopen:

- `kubectl config current-context` is `kind-rig-sandbox`;
- `/version` komt overeen met `git rev-parse --short HEAD`;
- `uv sync --all-groups` is schoon.

Let op de valkuil uit RC-110: na een deploy antwoorden de oude en de nieuwe pod even allebei. Wacht tot `/version` vijf keer achter elkaar de nieuwe commit geeft.

En de valkuil die die ronde een rode suite kostte: `E2E_SECRET_KEY` haal je zo op, en niet met een `jsonpath` op een sleutel die niet bestaat (dat geeft geen fout maar een lege string, en dan faalt alles op de inlogpagina):

```bash
kubectl -n rig-system get cm operations-manager-config -o jsonpath='{.data.\.env}' \
  | grep -E '^SECRET_KEY=' | cut -d= -f2-
```

## Taken

### 1. De suite draaien

`uv run pytest -m reallife -q` tegen het draaiende cluster. Reken op een uur.

Verifieer: nul failures. Faalt er een, dan **eerst uitzoeken of het aan de test of aan de code ligt**. Bij RC-108 zaten veertien van de vijftien bevindingen in de testlaag en bij RC-110 was de enige rode run een meetfout; dat is een reden om beter te kijken, niet om sneller te concluderen.

### 2. Jagen op punt 14

Draait de suite groen zonder dat `deployment_not_found` zich voordoet, dan is dat **geen bewijs dat het weg is**. Zoek het actief op:

- Bouw de toestand na die de zad-cli beschrijft: een project met meerdere componenten en een stapel wijzigingen met `rollout=false`, en dan een `deployment create`.
- Herhaal dat een aantal keer. Het is intermitterend, dus één groene poging zegt niets.
- Doe het terwijl de suite draait, zodat er werkelijk belasting is.

Reproduceer je het: leg de logregels vast rond de deelstap die faalt, met tijdstempels, en zoek uit welke lezing de oude stand teruggaf. Reproduceer je het niet: schrijf op wat je precies geprobeerd hebt en hoe vaak, zodat de volgende niet bij nul begint.

### 3. Wat de suite over gelijktijdigheid zegt

Dit is de eerste keer dat deze suite draait sinds de ProjectStore en het dienstensysteem er zijn. Let specifiek op:

- conflicten bij het opslaan (`ConflictError`, `ConcurrencyError`) — worden die netjes afgehandeld of komen ze als 500 naar buiten;
- taken die op elkaars projectbestand wachten;
- een tweede taak die een half doorgevoerde wijziging van een eerste te zien krijgt.

### 4. Het verslag

Kort, in `docs/`. Wat er gedraaid is, wat er groen was, en per bevinding of het de test of de code was. Punt 14 krijgt een eigen kopje met een expliciet oordeel: gereproduceerd en verklaard, gereproduceerd en niet verklaard, of niet gereproduceerd na zoveel pogingen.

Dat oordeel is waar deze taak voor bestaat, dus laat het niet in een opsomming verdwijnen.

## Wat er buiten valt

Repareren. Wordt punt 14 gereproduceerd en verklaard, dan is de reparatie een eigen taak met een eigen test. Deze taak levert het bewijs, niet de oplossing.
