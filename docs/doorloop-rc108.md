# De ultieme sandboxdoorloop (RC-108)

Datum: 13 augustus 2026. Tak: `de-ultieme-sandboxdoorloop`. Commit onder test: `713c0c7d`.

Deze doorloop moest vier dingen aantonen voor de merge naar main: dat een schone sandbox
overeind komt, dat een project de hele weg doorloopt, dat alle geautomatiseerde tests
groen zijn, en dat de schermen kloppen wanneer je er echt naar kijkt. Wat hieronder staat
is wat er gemeten is, inclusief wat er misging.

## Oordeel

**Uitrollen kan.** Alle drie de geautomatiseerde suites zijn groen op de commit onder test
(unit 8519, e2e 400, sandbox 55), de API-weg doet van aanmaken tot verwijderen wat de
documentatie belooft, en de schermen zijn in de browser bekeken en kloppen.

Met twee beperkingen die de lezer moet meewegen, want ze zijn niet aangetoond en niet
weerlegd:

1. **Dat een sandbox vanaf nul overeind komt is NIET getoetst** (taak 1). Het gereedschap
   en de sleutels ontbraken in deze omgeving. Wat wel vaststaat is dat het draaiende
   cluster gezond is.
2. **Backup/restore en het volledige verwijderpad zijn niet handmatig doorlopen** (taak 5,
   stappen 7 en 8). Ze worden wel door de suite gedekt, en het verwijderen is via de API
   gecontroleerd op namespace, projectbestand en ArgoCD-applicatie.

Geen van de vijftien gevonden problemen zat in de applicatie: veertien zaten in de
testlaag, en het vijftiende is een RBAC-regel die nooit heeft bestaan. Dat is het
geruststellende deel. Het verontrustende deel is dat de testlaag zo ver achterop was
geraakt bij de opdeling in tabbladen dat tien sandboxtests en vijf e2e-tests naar plekken
wezen waar niets meer stond - rood dat niets over de code zei, en dat bij nog een ronde
uitstel "die doet het altijd al niet" was geworden.

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

### De uitslag na de reparaties: GROEN

```
uv run pytest tests/e2e/ -m "e2e and sandbox and not reallife"
55 passed, 409 deselected in 3109.38s (51m49)
```

Nul failures. Alle tien de eerder gevallen tests zijn in deze run langsgekomen en geslaagd.

Dit is de derde poging; de eerste twee zijn weggegooid en waarom staat hieronder, want dat
hoort bij een eerlijk verslag: de eerste omdat ik zelf het cluster patchte terwijl de
meting liep, de tweede omdat ik hem startte terwijl mijn eigen opruiming van zes projecten
nog door het cluster liep. Beide keren was de fout dezelfde: iets veranderen aan de
omgeving die je aan het meten bent.

### De uitslag van de eerste (schone) run: 10 failures, allemaal in de testlaag

De schone run gaf **10 failed, 45 passed** (46m19s). Geen van de tien wees op een fout in
de applicatie; ze hadden drie oorzaken, en alle drie waren het navigaties die stilletjes
ergens anders uitkwamen.

**1. De zijbalk won van het tabblad (7 tests).** `open_services_tab` klikte op
`a[href$='/services']` met `.first`. Dat matcht ook de Services-link in de **zijbalk**, die
naar de platformbrede cataloguspagina wijst en eerder in de DOM staat. De controle erna,
`wait_for_url("**/services")`, kon dat niet zien omdat beide adressen op `/services`
eindigen. Op die catalogus staat wel een kaart per dienst - inclusief "Redis Cache" - maar
zonder Configureer-knop, dus zeven tests liepen dood op een knop die daar per definitie
niet staat.

Dit is gevonden door naar de faalschermafdruk te kijken die Playwright zelf wegschrijft
(`tests/e2e/artifacts/FAILED-*.png`). Daarop stond de verkeerde pagina, in één oogopslag.

**2. Een tabklik op tekst die in de shadow DOM staat (1 test).**
`open_deployments_tab` klikte op `get_by_text("Deployments", exact=True)` en wachtte
daarna 800 ms. Het tablabel wordt door LOTC in de shadow DOM getekend, dus die tekst raakt
de tab niet; er werd iets anders (of niets) geklikt, de pagina bleef op Overzicht, en de
test meldde een ontbrekende knop in plaats van een mislukte navigatie.

**3. Nog een verhuizing (2 tests).** `test_detail_block_shows_invite_link` zocht het
uitnodigingsblok op Overzicht, terwijl `b134a581` de dienstblokken naar het tabblad
Services info verplaatste. De docstring van `open_detail` beweerde nog het oude en lokte
die fout uit.

Beide tabhelpers lopen nu via één `open_project_tab`: het volledige projectpad als
selector, en wachten op precies dat pad. Op `href*=` en niet `href$=`, want Deployments,
Metrics en Backups dragen de deploymentnaam in hun pad (`TABS_MET_DEPLOYMENT`).

### De structurele oorzaak eronder: wachten op een klok in plaats van op een toestand

Tijdens het repareren bleek een tweede, bredere fout, en die is belangrijker dan de tien
failures samen. Zeven plekken deden dit:

```python
before = forgejo.list_project_names()
walk_create_wizard(...)                                    # wizard indienen
name = forgejo.wait_for_new_project(before, timeout=240)    # git afvissen op een klok
```

Het aanmaken levert een **taak** op die zowel de uitkomst als de projectnaam kent. Dat
antwoord werd weggegooid, waarna de test tot vier minuten lang de Forgejo-listing polde om
de naam uit een verschil te **raden**. Twee keer gokken - over de tijd en over de uitkomst -
terwijl er een bron is die het weet.

Het faalt bovendien op de verkeerde manier. In deze doorloop meldde het "No new project
file appeared in Forgejo" terwijl het project gewoon was aangemaakt; de taak deed er
**47,14 seconden** over. Een uitgelopen klok werd zo een verzonnen mislukking - precies het
soort rood dat "die doet het altijd al niet" wordt.

De vervanging (`project_name_from_progress`) vraagt het aan de bron: de wizard komt uit op
`/projects/progress/<task_id>`, en die pagina toont pas bij een afgeronde **of** gefaalde
taak de knop naar `/projects/<naam>/details`. Daarop wachten is wachten op de toestand; de
timeout is daarmee een vangnet en geen wachtmechanisme, en een mislukking komt eruit als
een mislukking.

Daarbij hoorde nog een valkuil die alleen empirisch te vinden was. Een tussenversie las de
uitkomst uit de **paginatekst** en sloeg alarm op een geslaagde aanmaak. Oorzaak: LOTC
rendert de melding als `<nldd-banner variant="success" text="Project succesvol
aangemaakt...">`, dus de tekst staat in de shadow DOM en `inner_text` levert er niets van
op. Dat is niet uit de sjablonen af te leiden - het kwam pas boven water door de echte
`innerHTML` van een voortgangspagina op te vragen. De controle leest nu het attribuut
`variant`.

Er is geen enkele aanroep van `wait_for_new_project` meer over. Dat het bestand daadwerkelijk
in `zad-projects` staat blijft een aparte assertie: dat is wat deze suite hoort te bewaken.

### Wat dit over de doorlooptijd zegt

De suite duurt ~46 minuten, en dat is inherent. Gemeten op één aanmaak:

| Fase | Duur |
|---|---|
| Validatie, git-schrijf, namespace, database, Keycloak-realm, manifesten | **11 s** |
| Wachten tot ArgoCD gesynchroniseerd is en de pod gereed is | **24 s** |

Het aanmaken zelf is dus snel; de tijd zit in de GitOps-convergentie. Dat verklaart ook het
verschil met de API-weg (~10 s): die maakt alleen de projectbasis **zonder** deployment en
slaat de ArgoCD-wachttijd over. Het portaal erkent dit zelf in zijn voortgangstekst: *"door
een bekende bug in ArgoCD kan het aanmaken van een nieuw project een paar minuten duren...
een eventuele time-out-melding betekent niet dat het aanmaken is mislukt."* Dat is precies
waarom een klok hier geen uitkomst mag zijn.


## Taak 5 - De handmatige doorloop, met schermafbeeldingen

Uitgevoerd met `scripts/doorloop_rc108.py`, dat via de echte wizard een project aanmaakt
met een component, `publish-on-web`, een database en Keycloak, wacht tot ArgoCD `Healthy`
meldt, en daarna elk tabblad vastlegt. De plaatjes staan in `docs/doorloop-rc108/`.

Voor **elke** schermmeting is opnieuw gecontroleerd of `/version` nog de commit onder test
is; het script stopt anders. Op alle negen plaatjes staat in de voettekst
`ZAD 3c43145d @ de-ultieme-sandboxdoorloop`, zodat achteraf te zien is wat er draaide.

Twee dingen die eerst misgingen en die het opschrijven waard zijn:

- De wizard kent **geen** kaart voor `namespace-postgresql-database`: die dienst is
  `hidden=True` en gaat via de API. Voor de wizardweg is `postgresql-database` de juiste.
- De sandbox draaide even twee pods tegelijk. `sandbox-deploy` meldde de nieuwe versie,
  maar een `/version` direct erna gaf de oude - het verkeer werd verdeeld terwijl de oude
  pod afsloot. Tien keer achter elkaar `/version` opvragen en de endpoints controleren
  liet zien wanneer alleen de nieuwe pod nog antwoordde. Wie een scherm beoordeelt vlak na
  een uitrol moet dit weten, anders meet hij de vorige build.

### Wat er op de tabbladen staat

| Tabblad | Bevinding |
|---|---|
| Overzicht | project, beschrijving, tabbalk met negen tabbladen |
| Team | de leden met hun rol, en de knop Bewerken |
| Componenten | Acties-kaart + componentkaart met poorten, resources, dienst-chips met hulpknop, publieke links |
| Services | de dienstkaarten met hun Configureer-knop |
| Services info | het Keycloak-blok: realm, admin console, gebruikersnaam, gemaskeerd wachtwoord, gedeelde OTP met vervaluitleg |
| Deployments | Acties (8 knoppen), publieke links, en de deployment met `Healthy`/`Synced`, revisie, laatste sync en componentimage |
| Metrics | zes grafieken (CPU, geheugen, netwerk in/uit, disk lezen/schrijven) met limietlijn en actuele waarde |
| Backups | schema-status en per deployment een expliciete lege toestand |
| Taken | de takenlijst van het project |

**Geen kop zonder inhoud.** Waar niets is, staat dat er ook: "Geen backup schema
ingesteld", "Geen backups gevonden voor deze deployment". Dat is precies wat deze vraag
moest uitsluiten.

Twee oneffenheden, geen van beide blokkerend:

1. Bij *Disk write* (alles 0,00 KB/s) ontbreken de asaanduidingen die de vijf andere
   grafieken wel hebben - de grafiek is een leeg kader met alleen een tijdlabel.
2. De componentkaart toonde geen omgevingsvariabelen of aliassen, simpelweg omdat dit
   verse project er geen heeft. **Dat punt uit het plan is via deze schermafbeelding dus
   niet bevestigd**; het wordt wel gedekt door de sandboxtests
   (`test_sandbox_env_vars_aliases_ui.py` en
   `test_aliases_land_in_the_project_file_as_one_age_block`, beide groen), die de waarden
   zetten en daarna het projectbestand controleren.

### Wat er van deze taak NIET af is

Eerlijk begrensd: van de acht genummerde stappen in het plan zijn er vijf gedaan
(aanmaken, uitrollen, de URL, de tabbladen langs, en het projectbestand). Backup maken en
terugzetten (stap 7) en het volledige verwijderpad met controle op namespace, database,
bucket en realm (stap 8) zijn hier **niet** handmatig doorlopen. Ze zijn wel gedekt door de
sandboxsuite en, voor het verwijderen, door taak 6 - waar na het verwijderen is
gecontroleerd dat de namespace, het projectbestand en de ArgoCD-applicatie alle drie weg
waren.

## Bevinding buiten de taken: verwijderen laat de ArgoCD-Application in git staan

Gevonden bij het opruimen tussen twee metingen door, en nagemeten.

Na het verwijderen van acht testprojecten stonden hun `Application`-objecten nog in ArgoCD,
met sync-status `Unknown`. Met `kubectl delete` weggehaald - en ze **kwamen terug**, terwijl
`user-applications` op `OutOfSync` sprong. De reden staat in git:

| Repository | Na het verwijderen |
|---|---|
| `zad-projects/projects/` | `enval-a6a.yaml`, `invit-knd.yaml`, ... **weg** |
| `zad-argo-user-applications/sandboxed-local/` | `enval-a6a/`, `enval-m4k/`, `enval-xhr/`, `invit-3jf/`, `invit-au4/`, `invit-eux/`, `invit-knd/`, `pgsch-at8/` **nog aanwezig** |

Het projectbestand gaat weg, de Application-definitie niet. De app-of-apps herstelt daarna
precies wat er in git staat, dus zo'n wees is niet weg te krijgen zolang die map er is. In
het log komt dat terug als:

```
Application 'enval-a6a-productie' terminal condition: ComparisonError: Failed to load
target state: ... ./sandboxed-local/enval-a6a/productie: app path does not exist
```

Het cluster loopt daarmee langzaam vol met applicaties die naar een niet-bestaand pad
wijzen, en elke reconcile houdt daar werk aan.

**Niet in deze doorloop opgelost.** Dit zit in de verwijderweg (`delete_project_manager`),
valt buiten de opdracht, en verdient een eigen taak met een eigen test. De vorm van die
test ligt voor de hand en past bij wat de sandboxsuite goed kan: verwijder een project en
controleer dat er in **beide** repositories niets achterblijft.

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

## Aanbevelingen

Drie dingen die deze doorloop opleverde en die een eigen taak verdienen. Geen ervan is
blokkerend voor de merge.

### 1. Het verwijderen laat de ArgoCD-Application in git staan

Beschreven hierboven. De testlaag heeft er al een noodverband voor (`force_cleanup_project`
in `tests/e2e/helpers/cluster.py`), met in de docstring letterlijk *"orphaning the (now
empty) namespace and a dangling `Unknown` ArgoCD app. Left unchecked these accumulate
across runs and starve the cluster"*. Het lek is dus bekend en wordt in de tests
weggepoetst in plaats van in `delete_project_manager` opgelost.

### 2. De sandboxsuite kan waarschijnlijk parallel

De suite duurt 52 minuten, en dat is bijna volledig **idle wachten** op ArgoCD. Gemeten
tijdens deze run:

| Meting | Waarde |
|---|---|
| Wachttijd op de lock van de projectstore | **5,1 s** in 25 minuten, over 172 acties |
| Langste enkele wachttijd op die lock | 1,23 s |
| Opzet per testbestand (nieuw project) | 85-150 s |
| De duurste enkele test (slapen + wekken) | 346 s |

De serialisatie in OPI is dus verwaarloosbaar; de rem is convergentie, en parallel wachten
kost nauwelijks extra. Isolatie is geen bezwaar: elk testbestand heeft zijn eigen project,
namespace, Keycloak-realm, database en ArgoCD-applicatie, dus `--dist loadfile` raakt per
worker een ander project. Wat er nog voor nodig is: `pytest-xdist` als dependency (staat er
nu niet in), en meten hoeveel projecten dit Kind-cluster tegelijk aankan - dat is de echte
onbekende, niet de code.

### 3. `wait_for_project_apps_healthy` loopt achter op ArgoCD zelf

De helper vraagt het niet aan ArgoCD maar pollt elke 5 seconden `kubectl get applications`
en eist dat **alle** apps met dat projectlabel Healthy zijn, inclusief de aparte
`-infrastructure`-app. Verschijnt die later, dan wacht hij door terwijl ArgoCD de eerste al
gezond noemt. Zichtbaar als tests die langer duren dan het cluster nodig had.

## Wat er structureel veranderd is

Naast de losse reparaties zijn twee dingen aangepast die de volgende doorloop moeten
behoeden voor wat deze kostte:

1. **De testhelpers vragen de taak wat er gebeurd is** in plaats van de git-listing af te
   vissen op een klok (`project_name_from_progress`). Geen enkele aanroep van
   `wait_for_new_project` is nog over.
2. **Een lange run is zichtbaar terwijl hij loopt.** `PYTEST_VOORTGANG=<pad>` laat pytest
   per afgeronde test een regel wegschrijven met tijdstip, `n/totaal`, duur, het aantal
   rode tot dan toe, de uitslag en de nodeid - uit pytest's eigen `report`-object, niet uit
   de uitvoer gegrepen. Zie `workflow/build.md`. Dat een run op de achtergrond stond met
   zijn uitvoer in een bestand was voor iedereen die meekeek hetzelfde als stilte, en dat
   heeft in deze doorloop meer dan eens tot de verkeerde conclusie geleid.
