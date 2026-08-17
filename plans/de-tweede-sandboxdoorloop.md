# De tweede sandboxdoorloop

Dezelfde vraag als RC-108: kan dit naar main. Nu op `1b53e869`, met alles erin wat er sinds die doorloop bij is gekomen, en met twee dingen die de vorige ronde open liet.

**Alles moet groen.** Niet "groen op de bekende falers".

## Wat er sinds RC-108 bij is gekomen

- **De ArgoCD-fix en het wachten eruit** (`1b53e869`). De vaste `sleep(5)` en `sleep(8)` in `simple_background.py` zijn weg, de readiness-probe staat op 2 seconden, de exists-check op 1 en de statuspoll op 2. Dit is de wijziging waar deze ronde het meest over moet zeggen.
- **Het polltempo in de wachtlussen** op 2 seconden, uit één constante (`POLL_INTERVAL_SECONDEN` in `argo_manager.py`).
- **De verouderde ArgoCD-waarschuwing** uit de takenlijst; de zin dat een time-out geen mislukking is, staat er nog.
- **De pull policy** van de sandbox-overlay op `IfNotPresent`.
- RC-107 (gebruik per project op het dashboard, met klikbare projectnamen) en RC-109 (een bijlage vervangen met een vaste id).

## De twee dingen die RC-108 openliet

Ze staan hier vooraan omdat ze de reden zijn dat het oordeel van die ronde twee slagen om de arm hield.

**1. Een sandbox vanaf nul.** Niet uitgevoerd, want het gereedschap en de sleutels ontbraken. Lukt dat nu niet, meld dan wat er precies ontbreekt in plaats van de taak stil over te slaan.

**2. Backup, restore en het volledige verwijderpad met de hand.** De suite dekt ze, maar niemand heeft ernaar gekeken.

## Meet de doorlooptijd, dat is deze keer de kern

RC-108 mat 42,9 seconden voor een heel project. Meet het opnieuw en zet het ernaast:

| Fase | RC-108 | Nu |
|---|---|---|
| Projectbestand, namespace, secrets, manifesten | 2s | |
| Wachten tot ArgoCD de applicatie aanmaakt | 6s | |
| Wachten tot synced en healthy | 21s | |
| ArgoCD-sync afronden | 13s | |
| **Totaal** | **42,9s** | |

De regels staan in de OPI-logs: `Waiting for ArgoCD application ... to be created`, `Infrastructure status: sync=..., health=...` en `Task ... completed successfully in ...s`.

Kijk niet alleen naar het totaal maar ook naar **wat de gebruiker ziet**. Het verschil tussen "klaar" en "de UI zegt klaar" is nu maximaal 2 seconden in plaats van 5. Als de wizard nog steeds traag voelt terwijl de kloktijd goed is, dan zit het in het verversen van de voortgang in de browser, en dat is een eigen bevinding waard.

## De vaste taken

### 1. Schone sandbox

`task sandbox:destroy` en `task sandbox:setup`. Verifieer: pods draaien, ArgoCD bereikbaar, Forgejo heeft de drie repositories, het portaal geeft een inlogscherm. Noteer de duur.

### 2, 3, 4. De drie suites

- `uv run pytest tests/ -q` met de eigen standaardaanroep (geef **geen** eigen `-m` mee; de standaard deselecteert `requires_infra` en `e2e` zelf, en met een eigen `-m` zet je infrastructuurtests aan die hier niet kunnen draaien).
- `uv run pytest -m e2e -q`
- `uv run pytest -m sandbox -q` tegen het draaiende cluster.

Verifieer per suite: nul failures, nul errors. Faalt er een, **eerst uitzoeken of het aan de test of aan de code ligt** en dat opschrijven. Bij RC-108 zaten veertien van de vijftien bevindingen in de testlaag; dat is een reden om beter te kijken, niet om sneller te concluderen.

### 5. De handmatige doorloop

`scripts/doorloop_rc108.py` doet dit al en controleert `/version` bij elke schermmeting. Hergebruik dat script.

Elk tabblad langs, en per tabblad de vraag: staat er wat er hoort te staan, en is er ergens een kop zonder inhoud? Verder:

- Componenten: omgevingsvariabelen en aliassen in de kaart van hun component, en de hulpknop bij het aliassenveld toont de variabelen per dienst.
- Dashboard: **Gebruik per project** staat onder Resourcegebruik, met geheugen boven CPU, gesorteerd op geheugen, en de projectnamen zijn links die werken.
- Bijlagen: een bestaande bijlage **vervangen**, en daarna controleren dat de koppeling van het component er nog is. Dat is de hele reden van RC-109.
- Een component bewerken en opslaan; daarna in de projectenrepository controleren dat het realm-wachtwoord er nog staat en de aliassen als **één AGE-blok** zijn weggeschreven.
- Backup maken en terugzetten (de openstaande taak uit RC-108).
- Project verwijderen en controleren dat namespace, database, bucket en Keycloak-realm echt weg zijn.

### 6. De API-weg

Aanmaken, dienst toevoegen, component toevoegen, uitrollen, opvragen, verwijderen. Neem de twee punten van de zad-cli mee:

- `deployment describe` draagt `source` en `pending_rollout`, en die laatste is `null` in een lijst en gevuld op het enkele antwoord.
- Een restore met een onbekende referentie noemt in de 404 namen die `backup list` ook teruggeeft.

### 7. Het verslag

Eén document in `docs/`, met per taak wat er gedaan is, wat er gemeten is, en wat er misging. **Ook wat er misging.** De tabel met doorlooptijden hoort erin. Sluit af met een oordeel in één zin: uitrollen kan, of uitrollen kan niet en dit is waarom.

## Als iets rood is

Stoppen, uitzoeken, opschrijven, dan pas verder. Deze doorloop beantwoordt of we naar main kunnen, en één rode test die "waarschijnlijk niets is" maakt dat antwoord onbetrouwbaar.

## Wat er buiten valt

De `reallife`-suite, productie, en nieuwe functionaliteit. Wat hier gevonden wordt en niet blokkerend is, wordt een eigen taak.
