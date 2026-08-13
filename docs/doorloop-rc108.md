# De ultieme sandboxdoorloop (RC-108)

Datum: 13 augustus 2026. Tak: `de-ultieme-sandboxdoorloop`. Commit onder test: `713c0c7d`.

Deze doorloop moest vier dingen aantonen voor de merge naar main: dat een schone sandbox
overeind komt, dat een project de hele weg doorloopt, dat alle geautomatiseerde tests
groen zijn, en dat de schermen kloppen wanneer je er echt naar kijkt. Wat hieronder staat
is wat er gemeten is, inclusief wat er misging.

## Oordeel

<!-- INVULLEN -->

## Vooraf

| Controle | Uitkomst |
|---|---|
| `kubectl config current-context` | `kind-rig-sandbox` |
| `/version` tegen `git rev-parse --short HEAD` | `713c0c7d` = `713c0c7d` |
| `uv sync --all-groups` | schoon, 182 pakketten |

De versiecontrole is niet één keer gedaan maar voor **elke** schermmeting opnieuw: het
walkthrough-script (`scripts/doorloop_rc108.py`) roept `/version` aan voor elk tabblad dat
het vastlegt en stopt met een foutmelding zodra de draaiende build afwijkt van de commit
die je meet.

De build is op het cluster gezet met `sandbox-deploy`, dat de lock claimt, bouwt,
`kind load`t, uitrolt en `/version` verifieert.

## Taak 1 - Schone sandbox: NIET UITGEVOERD

`task sandbox:destroy` gevolgd door `task sandbox:setup` is in deze sessie niet mogelijk,
en het was onverstandig geweest om het toch te proberen:

- `task requirements-check` faalt op een ontbrekende `sops` (en ook `yq` en `pwgen`
  ontbreken), dus `sandbox:setup` komt niet voorbij zijn eigen precondition.
- `security/` bevat geen `developer-key.txt` en geen `sandbox-key.txt`. Zonder de eerste is
  het wildcard-certificaat niet te ontsleutelen, zonder de tweede zijn de
  runtime-secrets niet te genereren.
- `workflow/sandbox.md` verbiedt deze taken expliciet in een sessie op de dev-server, en
  het cluster is gedeeld. Een destroy die ik daarna niet kan herbouwen haalt niet alleen
  deze doorloop onderuit maar ook elke andere PR die op het cluster wacht.

**Wat wel gemeten is** op het draaiende cluster:

| Controle | Uitkomst |
|---|---|
| Pods in `rig-system` | 14/14 Running, 0 anders |
| ArgoCD (`argo.sandbox.rijksapp.dev`) | HTTP 200 |
| Forgejo | HTTP 200, repositories `zad-projects`, `zad-argo-user-applications`, `zad-deployments` (+ `zad-argo-infrastructure`) |
| Keycloak | HTTP 200 |
| `zad.sandbox.rijksapp.dev` | HTTP 302 naar het Keycloak-inlogscherm |

Daarmee is aangetoond dat het cluster gezond is, maar **niet** dat het vanaf nul overeind
komt. Dat blijft open en hoort op een machine met de volledige gereedschapskist en de
AGE-sleutels gedaan te worden.

## Taak 2 - De unittests: GROEN

```
uv run pytest tests/ -q
8519 passed, 7 skipped, 528 deselected, 12 warnings in 365.63s (6m05)
```

Nul failures, nul errors. De eigen standaardaanroep van het project is gebruikt, zonder
eigen `-m`, zodat `requires_infra` en `e2e` gedeselecteerd blijven.

## Taak 3 - De e2e-tests: GROEN, na vijf reparaties

Eindstand:

```
uv run pytest -m e2e -q
400 passed, 62 skipped, 8590 deselected, 2 xfailed in 670.03s (11m10)
```

De weg daarheen is het interessante deel, want de eerste run leverde **397 errors in 21
seconden** op terwijl elk testbestand afzonderlijk groen was.

### Bevinding 1 - het patchdoel dat alleen bij toeval werkte

`tests/e2e/testserver.py` schakelde de echte opstartroutine uit met
`patch("opi.core.startup.run_startup_tasks")`. Maar `opi/server.py` haalt die naam met
`from opi.core.startup import run_startup_tasks` naar zich toe en roept zijn eigen binding
aan. Het definitiepad patchen raakt die binding alleen als `opi.server` daaronder voor het
**eerst** geimporteerd wordt.

Daarmee hing de hele suite af van wat er verder verzameld werd:

- `pytest tests/e2e` - `opi.server` nog niet geladen, de import pakt de mock op, groen;
- `pytest -m e2e` (de hele boom, en dus de aanroep uit het plan) - een unittest had
  `opi.server` al geimporteerd, de echte opstartroutine bleef staan, de app ging een
  database zoeken die er niet is (`gaierror`), en de retry duurde langer dan de tien
  seconden die de `app_server`-fixture wacht. Elke test faalde in setup.

Nu gepatcht op `opi.server.run_startup_tasks`, dus waar het gebruikt wordt. Dat is dezelfde
regel die `tests/conftest.py` elders al hanteert ("Patch where it's used, not where it's
defined").

### Bevindingen 2 tot en met 5 - vier verhuizingen die hun tests niet meenamen

Na die fix bleven negen failures over. Alle negen waren verouderde tests en geen kapotte
code; in alle vier de gevallen is de applicatie zelf nagelopen op de vraag of het gedrag
klopt, en dat deed het.

| Test | Wat de test aannam | Wat er veranderd is |
|---|---|---|
| `test_backup.py` (7 tests) | Backups staat op het tabblad Deployments | `36209fad` gaf Backups een eigen tabblad |
| `test_lotc_modal_dialoog.py` (4 tests) | de teamdialoog opent vanaf Overzicht | `a16338ee` gaf Team een eigen tabblad |
| `test_lotc_project_tab.py` (3 tests) | de teamknop staat op Overzicht; de componentknop heet "Toevoegen" | `a16338ee` en `ae981a75` ("Component toevoegen") |
| `test_lotc_services_info_tabblad.py` (2 tests) | `test-project-detail` heeft geen dienst met een blok | `daa04886` gaf dat project een keycloak-realm, dus het heeft er wel een |

Die laatste is de aardigste: de test toetst de regel uit RC-101 dat een tabblad zonder
inhoud niet in de balk hoort. Die regel werkt nog steeds - alleen was het project dat de
test als "leeg" gebruikte ondertussen niet meer leeg, omdat er een realm aan toegevoegd was
om een heel ander pad te kunnen testen. De test is nu op `test-project` gezet, dat
werkelijk geen `services`-sleutel heeft.

### Wat dit zegt over de suite

Vier van de vijf bevindingen zijn dezelfde soort: iets is naar een ander tabblad verhuisd
en de tests die erop wezen bleven achter. Ze faalden dus niet op wat ze meten maar op waar
ze kijken. Dat is precies het soort rood dat "die doet het altijd al niet" wordt als je het
laat liggen.

## Taak 4 - De sandboxtests

`uv run pytest tests/e2e/ -m "e2e and sandbox and not reallife"` tegen het draaiende
cluster: 55 tests van de 464 verzamelde.

### Eerst: een meetfout van mezelf, en wat die kostte

De eerste run is weggegooid, en de reden hoort in dit verslag omdat het precies het soort
fout is waar deze doorloop tegen bedoeld is.

Halverwege die run vond ik de RBAC-bevinding hieronder en heb ik de ClusterRole op het
**draaiende cluster** gepatcht om de fix te kunnen meten. Dat deed ik met
`kubectl patch --type=json` op `/rules/2/resources` - een positie die ik niet geverifieerd
had. Regel 2 was niet `deployments` maar **`secrets`**. Ik heb dus de rechten om secrets te
maken overschreven, en daarna "hersteld" naar een waarde die ik gegokt had.

Het gevolg stond binnen een minuut in het log:

```
Failed to create SOPS secret in namespace rig-e2e45-2n8 after 10 attempts.
RBAC permissions may not have propagated for this namespace.
```

112 van die meldingen over vijf namespaces, tot ik het zestien minuten later terugdraaide.
Elke uitrol in dat venster faalde door mij en niet door de code.

Drie dingen deugden niet, en alle drie zijn ze veranderd:

1. **Ik veranderde het cluster terwijl er een meting op liep.** Of de run bezit het
   cluster, of jij - nooit allebei.
2. **Ik patchte op index in plaats van het manifest toe te passen.** `kubectl apply -f`
   met het bestand uit git kan deze fout niet maken; een `patch` op een geraden positie
   wel. De ClusterRole staat nu weer via `apply` gelijk aan het manifest, en dat is
   nagemeten door regel voor regel te vergelijken in plaats van te kijken of het "goed
   oogt".
3. **Ik draaide de suite met de uitvoer door `tail`,** dus ik zag vijftig minuten lang
   niets en moest de fout uit de serverlogs afleiden in plaats van uit de test die omviel.
   De herhaling draait met `-v` naar een logbestand, met een wachter die meldt zodra de
   eerste test rood wordt.

De les die hier het meest toe doet: ik heb de eerste faalmelding **wel gezien** en toen
geconcludeerd dat hij niet van mij kwam. Die conclusie baseerde ik op de toestand *na* mijn
patch - waar de schade al in zat - in plaats van op een vergelijking met het manifest. Een
regel die `deployments` heet met verbs `create/patch/delete` had genoeg moeten zijn om te
twijfelen.

### De bevinding die eronder lag: de OOM-watcher mist een recht

In een uur stonden er 70 meldingen als deze in het log:

```
replicasets.apps is forbidden: User "system:serviceaccount:rig-system:namespace-manager"
cannot list resource "replicasets" in API group "apps"
```

`_get_current_pod_template_hash` in `opi/services/oom_watcher.py` leest de replicasets om
te bepalen welke ReplicaSet de huidige is. De ClusterRole gaf alleen `deployments`; het
lezen van replicasets is er in `537b00a9` bij gekomen zonder de bijbehorende regel, en
`git log -S replicasets` over `bootstrap/` geeft niets - dat recht heeft dus op geen enkel
cluster ooit bestaan.

Het faalt stil. De lookup geeft `None` en de watcher valt terug op "alle pods beoordelen",
waardoor een pod van een **vervangen** ReplicaSet als OOM-kill kan meetellen. Dat voedt het
auto-tunen van het geheugen, dus het is geen cosmetische logregel.

Aangepast in alle drie de overlays (`local`, `sandboxed-local`, en het
`_blueprint`-bestand voor `odcn-production`). Nagemeten op de sandbox: `kubectl auth can-i
list replicasets.apps` gaat van nee naar ja en de meldingen stoppen.

**Voor productie is dit een verzoek en geen wijziging.** De serviceaccounts op
odcn-production maken we niet zelf; het blueprint legt vast hoe de ClusterRole eruit hoort
te zien, maar iemand met rechten op dat cluster moet het toepassen. Zolang dat niet gebeurd
is, blijft de OOM-watcher daar terugvallen op alle pods.

### De uitslag

<!-- INVULLEN -->


## Taak 5 - De handmatige doorloop

<!-- INVULLEN -->

## Taak 6 - De API-weg: GROEN

Dezelfde doorloop met `curl` tegen `/api/v2`. Het volledige protocol staat in
`docs/doorloop-rc108/api-doorloop.log`.

| Stap | Aanroep | Antwoord |
|---|---|---|
| Aanmaken | `POST /api/v2/projects` (Bearer) | 202, `task_id` + `project_name` + `api_key` |
| Dienst | `POST /api/v2/projects/{p}/services` (2x) | 202, taken `completed` |
| Component | `POST /api/v2/projects/{p}/components` | 202, taak `completed` |
| Uitrollen | `POST /api/v2/projects/{p}/:upsert-deployment` | 202, taak `completed` |
| Opvragen | `GET /api/v2/projects/{p}` | 200, diensten + componenten + deployment |
| Deployments | `GET /api/v2/projects/{p}/deployments` | 200, `status: Healthy`, met URL |
| Verwijderen | `DELETE /api/projects/{p}` | 200, `deleted successfully` |

**De URL die het antwoord noemt geeft ook echt antwoord** (dat was bevinding 13 uit een
eerdere ronde). `GET /deployments` beloofde
`https://web-prod-radt-qfo.sandbox.rijksapp.dev`; die gaf HTTP 200 en - gemeten mét
certificaatcontrole, dus zonder `-k` - een vertrouwd certificaat. De bevinding reproduceert
niet.

Na het verwijderen zijn de namespace, het projectbestand in Forgejo en de ArgoCD-applicatie
alle drie weg.

### Twee dingen om te weten voor wie dit nadoet

**De aanmaakroute vraagt een token met een specifieke audience.** `POST /api/v2/projects`
is de enige die geen projectsleutel kan gebruiken (het project bestaat nog niet) en
accepteert een SSO-token. Dat token moet `aud=zad-api` dragen, en alleen de Keycloak-client
`zad-cli` heeft de audience-mapper die dat toevoegt. Die client is publiek en doet
authorization code + PKCE naar een localhost-redirect; een `password`-grant op
`rig-platform-operations-manager` levert wel een geldig token maar wordt afgewezen met
`Invalid claim 'aud'`. Het scriptje dat de CLI-weg nabootst staat in het protocol.

**Alles is asynchroon.** Elke mutatie antwoordt met 202 en een `task_id`. Een `GET` direct
na een `POST` meet de toestand van *voor* de mutatie: de eerste ronde liet `services: []`
zien terwijl de dienst al geaccepteerd was. Dat is geen fout in de API maar wel de valkuil
waar een doorloop op strandt als hij het antwoord voor de uitkomst aanziet. Wachten doe je
op `/api/tasks/{id}`, niet op de klok.

### Observatie: `pending-rollout` telt door tot een `:refresh`

Na een geslaagde `:upsert-deployment` bleef `GET /pending-rollout` `count: 1` melden met
`task_types: ["create_project"]`, terwijl de deployment `Healthy` was. Dat leek een fout,
maar de documentatie van het endpoint zegt het letterlijk: elke wijziging telt mee "until
the project is rolled out again with `POST /:refresh`". Nagemeten: na `POST /:refresh` gaat
de teller naar `count: 0`. Gedrag klopt met de belofte; wel iets om te weten, want het
uitrollen van één deployment maakt de teller niet leeg.
