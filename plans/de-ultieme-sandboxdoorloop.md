# De ultieme sandboxdoorloop

Voor de merge naar main: één volledige uitrol op het sandboxcluster, van niets tot een draaiend project, en daarna alles wat we hebben aan tests. **Alles moet groen.** Niet "groen op de bekende falers", niet "die twee doen het altijd al niet". Groen.

Het vertrouwen is laag na een dag waarin schermwijzigingen niet aankwamen en een dashboardkaart onderweg is gesneuveld. Deze doorloop is er om dat vertrouwen met metingen te herstellen, niet met beweringen.

## Wat het moet aantonen

1. Een schone sandbox komt vanaf nul overeind.
2. Een project doorloopt de hele weg: aanmaken, uitrollen, gebruiken, wijzigen, verwijderen.
3. Alle geautomatiseerde tests zijn groen: unit, e2e en sandbox.
4. De schermen kloppen, gemeten in de browser en niet gelezen in de markup.

## Vooraf

Wat de vorige rondes gekost heeft en hier dus als eerste gecontroleerd wordt:

- **De kubectl-context.** `kubectl config current-context` moet `kind-rig-sandbox` zijn. Staat hij op productie, dan gaat een sync daarheen en lijkt alles stil te staan. Sinds `5459c737` staat de context in de skaffold-bestanden zelf; controleer dat de draaiende skaffold dat bestand ook gebruikt.
- **De draaiende build.** `curl -sk https://zad.sandbox.rijksapp.dev/version` en vergelijk `commit` met `git rev-parse --short HEAD`. Wijken die af, dan test je iets anders dan wat er in git staat, en dan is elk oordeel over een scherm waardeloos. **Doe deze controle opnieuw voor elke schermmeting**, niet één keer aan het begin.
- **Testafhankelijkheden.** `uv sync --all-groups` in een verse worktree, anders faalt de pre-push hook op een ontbrekende module.

## Taken

### 1. Schone sandbox

`task sandbox:destroy` en daarna `task sandbox:setup`. Vanaf nul, niet op een bestaand cluster: de helft van wat we vandaag zochten kwam voort uit toestand die er nog stond.

Verifieer: alle pods in `rig-system` draaien, ArgoCD is bereikbaar, Forgejo heeft de drie repositories, `https://zad.sandbox.rijksapp.dev` geeft een inlogscherm.

Noteer hoe lang dit duurde. Bij de vorige generale duurde de hele doorloop 1u25; als dit deel veel langer duurt is dat op zichzelf een bevinding.

### 2. De unittests

`uv run pytest tests/ -q` met de eigen standaardaanroep van het project (die deselecteert `requires_infra` en `e2e` zelf; geef **geen** eigen `-m` mee, want dan zet je infrastructuurtests weer aan die hier niet kunnen draaien).

Verifieer: nul failures, nul errors. Een test die overslaat is goed, een test die faalt niet.

### 3. De e2e-tests

`uv run pytest -m e2e -q`. Die draaien tegen een eigen server en hebben de sandbox niet nodig.

Verifieer: nul failures. Let bij een falende schermtest op of de meting faalt of de vormgeving; die twee vragen een ander antwoord.

### 4. De sandboxtests

`uv run pytest -m sandbox -q` tegen het draaiende cluster. `reallife` valt hier buiten, dat is een eigen lange suite.

Verifieer: nul failures. Faalt er een, dan **eerst uitzoeken of het aan de test of aan de code ligt** en dat opschrijven; een test aanpassen omdat hij rood is, is precies hoe een suite waardeloos wordt.

### 5. De handmatige doorloop, met schermafbeeldingen

`scripts/kijk_sandbox.py <pad>` maakt van elke stap een plaatje. **Kijk er ook echt naar**; dat is vandaag twee keer misgegaan en beide keren stond het probleem gewoon in beeld.

1. Een nieuw project via de wizard, met minstens een component, publish-on-web, een database en Keycloak.
2. Uitrollen en wachten tot ArgoCD `Healthy` meldt.
3. De URL van het component openen: hij moet antwoorden, geen 404 en geen certificaatfout.
4. De projectpagina langs, elk tabblad: Overzicht, Team, Componenten, Services, Services info, Deployments, Metrics, Backups, Taken. Per tabblad de vraag: staat er wat er hoort te staan, en is er ergens een kop zonder inhoud?
5. Op Componenten: de omgevingsvariabelen en de aliassen staan in de kaart van hun component. De hulpknop bij het aliassenveld opent de lijst met variabelen per dienst.
6. Een component bewerken en opslaan. Controleer daarna in de projectenrepository dat het realm-wachtwoord er nog staat en dat de aliassen als **één AGE-blok** zijn weggeschreven (RC-106) en niet per sleutel.
7. Een backup maken en terugzetten.
8. Het project verwijderen; controleer dat de namespace, de database, de bucket en de Keycloak-realm echt weg zijn.

### 6. De API-weg

Dezelfde doorloop met `curl` tegen `/api/v2`, want de CLI leunt daarop en heeft er dit jaar dertien bevindingen op gemeld. Minimaal: project aanmaken, dienst toevoegen, component toevoegen, uitrollen, opvragen, verwijderen.

Verifieer: elk antwoord is wat de documentatie belooft, en een URL die het antwoord noemt geeft ook echt antwoord (dat was bevinding 13).

### 7. Het verslag

Eén document in `docs/` met per taak: wat er gedaan is, wat er gemeten is, en wat er misging. **Ook wat er misging.** Een doorloop die alleen successen opsomt is geen doorloop.

Sluit af met een expliciet oordeel in één zin: uitrollen kan, of uitrollen kan niet en dit is waarom.

## Wat er buiten valt

- De `reallife`-suite (vijf projecten, semi-gelijktijdige mutaties). Die is een eigen traject.
- Productie. Deze doorloop gaat alleen over de sandbox.
- Nieuwe functionaliteit. Wat hier gevonden wordt en niet blokkerend is, wordt een eigen taak en geen sluipende uitbreiding van deze.

## Als iets rood is

Niet doorlopen en aan het eind samenvatten. Stoppen, uitzoeken, opschrijven, en dan pas verder. De vraag die deze doorloop beantwoordt is of we naar main kunnen; één rode test die "waarschijnlijk niets is" maakt dat antwoord onbetrouwbaar, en dat is precies wat we niet nog een keer moeten hebben.
