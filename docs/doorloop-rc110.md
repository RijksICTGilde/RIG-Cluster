# De tweede sandboxdoorloop (RC-110)

Datum: 13/14 augustus 2026. Tak: `de-tweede-sandboxdoorloop`.
Commits onder test: `b0541eb9` (taken 1 t/m 6) en `74db82f0` (dezelfde tak plus de reparatie
uit taak 5; de suites en de laatste meting draaien hierop).

Dezelfde vraag als RC-108: kan dit naar main. Nu met de doorlooptijd als kern, en met de
twee dingen die de vorige ronde openliet.

## Oordeel

**Uitrollen kan niet zonder eerst de restore-weg te repareren.** Al het andere is groen -
de drie suites, de schermen, de API-weg, het verwijderpad - maar een restore meldt
`success` en laat het project achter met een databasegeheim dat niet meer werkt, waarna
**elke** volgende wijziging op dat project faalt. Dat is nieuw ten opzichte van main en het
is niet zichtbaar voor wie alleen naar het antwoord van de API kijkt.

Twee kleinere zaken zijn in deze doorloop wel gerepareerd: de kaart **Pods** op het
dashboard stond altijd op 0, en `ruff format --check` was rood op de taktip.

## Vooraf

| Controle | Uitkomst |
|---|---|
| `kubectl config current-context` | `kind-rig-sandbox` |
| `/version` vs `git rev-parse --short HEAD` | `b0541eb9` = `b0541eb9`, later `74db82f0` = `74db82f0` |
| `uv sync --all-groups` | schoon, 182 pakketten |
| `ruff check` / `pyright` | schoon, 0 fouten |

De versiecontrole is voor **elke** schermmeting opnieuw gedaan: `scripts/doorloop_rc108.py`
roept `/version` aan per tabblad en stopt zodra de draaiende build afwijkt. Op elke
schermafbeelding staat de commit in de voettekst.

Eén ding om te weten voor wie dit nadoet: na `sandbox-deploy` antwoordden de oude en de
nieuwe pod even allebei. `sandbox-deploy` meldde zelf `WARN - /version does not clearly
show 74db82f0`. Wachten tot `/version` vijf keer achter elkaar de nieuwe commit geeft is
genoeg; wie dat overslaat meet de vorige build. RC-108 liep tegen hetzelfde aan.

## De doorlooptijd - de kern van deze ronde

Vier metingen van een volledige projectaanmaak via de wizard (component, `publish-on-web`,
database, Keycloak). De fasegrenzen komen uit de logregels die het plan noemt:
`Executing task ... type=create_project`, `Waiting for ArgoCD application ... to be
created`, `... to be synced and healthy` en `Project creation completed successfully`.

| Fase | RC-108 | 1 (leeg) | 2 (1 project draait) | 3 (2 projecten) | 4 (leeg, na fix) |
|---|---|---|---|---|---|
| Projectbestand, namespace, secrets, manifesten | 2s | 14,0s | 12,3s | 12,0s | 12,6s |
| Wachten tot ArgoCD de applicatie aanmaakt | 6s | 8,3s | 8,6s | 8,6s | 8,4s |
| Wachten tot synced en healthy | 21s | 35,9s | 47,0s | 54,0s | 27,2s |
| ArgoCD-sync afronden | 13s | **0,09s** | **0,05s** | **0,05s** | **0,07s** |
| **Totaal** | **42,9s** | **58,3s** | **68,0s** | **74,6s** | **48,2s** |

### Wat dit zegt

**De wijziging doet precies wat hij belooft.** De laatste fase - de twee vaste `sleep`s in
`simple_background.py`, samen 13 seconden - is nu **0,05 tot 0,09 seconde**. In het log is
het één regel:

```
01:06:14,173  Application 'e2e62-glv-productie' is synced and healthy
01:06:14,222  Starting ArgoCD monitoring for project: e2e62-glv
01:06:14,249  All project ArgoCD apps healthy for e2e62-glv
01:06:14,250  Project creation completed successfully (took 58.27s)
```

Dertien seconden wachten zijn zevenenzeventig milliseconden lezen geworden. Dat is de
winst, en die is hard.

**Het totaal is er niet door gedaald, en dat is geen tegenspraak.** De tijd zit in de
GitOps-convergentie (fase 3), en die schaalt met wat er verder op het cluster draait: 35,9s
op een leeg cluster, 47,0s met één ander project erbij, 54,0s met twee. Deze sandbox draait
op vier cores; elk extra project is een extra pod die om dezelfde cores vraagt. Wie de 42,9s
van RC-108 naast de 58,3s van nu legt, vergelijkt twee metingen op verschillende belasting,
niet twee versies van de code.

**De RC-108-kolom klopt bovendien niet met het RC-108-verslag zelf.** Dat verslag noemt voor
dezelfde aanmaak *11 s* voor de eerste fase en *24 s* voor het wachten - samen 35s, niet
42,9s met 2s voorop. De 2s in de plantabel is niet te rijmen met wat die fase doet: twee
git-pushes (elk ~1,5s), een Keycloak-realm met negen mappers en twee clients (~4s), de
manifesten en de SOPS-versleuteling (~2s), plus het schrijven van de ArgoCD-applicatie
(~4s). Twaalf tot veertien seconden is wat daar in dit cluster voor staat, vier metingen op
een rij. **De vergelijking met RC-108 is dus alleen op de laatste fase betrouwbaar**, en
juist die is de wijziging die getoetst moest worden.

### Wat de gebruiker ziet

De voortgangspagina ververst met `hx-trigger="every 2s"`
(`opi/templates_lotc/partials/task_progress_fragment.html.j2` en de wizardvariant). Het gat
tussen "klaar" en "de UI zegt klaar" is dus maximaal 2 seconden, zoals het plan stelt. Bij
de metingen hierboven kwam de knop naar het project telkens binnen dat venster; er is geen
aanleiding om het verversen als aparte oorzaak van traagheid aan te wijzen.

### Bijvangst: de gezondheidsprobe versnelde stilletjes mee

`POLL_INTERVAL_SECONDEN` ging naar 2, en de callback die per poll draait
(`_on_progressing` in `project_manager.py`) roept de OOM/health-inspectie aan. De
commit-toelichting zegt dat de zwaardere componenten-inspectie zijn eigen 5s-ritme houdt.
Dat geldt voor de `describe`-tak (die throttlet zichzelf op `describe_next`), maar **niet**
voor `check_all_components_health`: die zit achter
`HEALTH_CHECK_INTERVAL_SECONDS = 0` in `opi/services/oom_watcher.py`, en 0 betekent "elke
poll". In het log staat de probe dan ook elke ~2,2 seconde in plaats van elke 5:

```
01:05:43,189  Health check: probing 1 component(s) in rig-e2e62-glv
01:05:45,539  Health check: no issues detected
01:05:47,721  Health check: no issues detected
```

Elke probe is een `kubectl`-aanroep van ~175ms, en die blokkeert de eventloop
(bekend punt in deze codebase, zie `tests/e2e/test_no_kubectl_probe.py`). Het is 2,5 keer
zo vaak als bedoeld, tijdens precies de fase die de doorlooptijd bepaalt. De commentaarregel
bij de constante zegt letterlijk `Set to 0 to check every poll iteration (every 5s)` - die
parenthese is sinds `1b53e869` niet meer waar.

Niet gerepareerd: of dit ritme goed of fout is, is een keuze (sneller falen zien tegenover
minder cluster-belasting), en die hoort met een meting bij een eigen taak. Wel opgeschreven,
want het is een gedragsverandering die niemand heeft besloten.

## Taak 1 - Schone sandbox: NIET UITGEVOERD

Net als in RC-108, en om dezelfde redenen. Wat er **precies** ontbreekt:

| Wat | Stand |
|---|---|
| `sops` | ontbreekt (`task requirements-check` faalt hierop als eerste) |
| `yq`, `pwgen` | ontbreken ook |
| `security/developer-key.txt` | ontbreekt - nodig om het wildcard-certificaat te ontsleutelen |
| `security/key.txt` / `sandbox-key.txt` | ontbreken - nodig voor de runtime-secrets |

De ontbrekende gereedschappen zijn oplosbaar: `sops` is gewoon te downloaden (getoetst: de
release-binary haalt HTTP 200 in deze omgeving). De sleutel is dat niet. `task
sandbox:setup` roept als tweede stap `sandbox:decrypt-wildcard-cert` aan, en die doet zonder
`security/developer-key.txt` een interactieve `read -rs` om de AGE-sleutel te vragen. In een
sessie zonder tty levert dat een lege invoer op, en dan is het `exit 1` op
`Invalid key format`. De sleutel komt per definitie van buiten de repo.

Daar komt bij dat het cluster **gedeeld** is en `workflow/sandbox.md` deze taken in een
sessie expliciet verbiedt. Een `destroy` die ik niet kan terugbouwen haalt ook elke andere
PR onderuit die op het cluster wacht.

**Wat wel gemeten is** op het draaiende cluster:

| Controle | Uitkomst |
|---|---|
| Pods in `rig-system` | 14/14 Running |
| ArgoCD, Forgejo, Keycloak | HTTP 200 |
| MinIO / Prometheus | 403 / 302 (beide antwoorden van een levende dienst) |
| Portaal `zad.sandbox.rijksapp.dev` | HTTP 302 naar `/auth/login` |
| Forgejo-repositories | `zad-projects`, `zad-argo-user-applications`, `zad-deployments` (+ `zad-argo-infrastructure`) |

Het cluster is dus gezond. Dat het **vanaf nul** overeind komt blijft onbewezen, en hoort op
een machine met de sleutels gedaan te worden. Dit is nu twee rondes op rij niet getoetst;
het verdient een eigen taak op een machine die het wel kan, niet nog een vermelding.

## Taak 2 - De unittests: GROEN

```
uv run pytest tests/ -q
8551 passed, 7 skipped, 528 deselected, 20 warnings in 344.50s (5m44)
```

Nul failures, nul errors, met de eigen standaardaanroep (geen eigen `-m`, dus
`requires_infra` en `e2e` blijven gedeselecteerd).

## Taak 3 - De e2e-tests: GROEN

```
uv run pytest -m e2e -q
400 passed, 62 skipped, 8626 deselected, 2 xfailed, 33 warnings in 664.29s (11m04)
```

Nul failures, nul errors, in één run. Dat is het verschil met RC-108: daar kostte deze
suite vijf reparaties in de testlaag voordat hij groen was. Die reparaties zitten er nu in
en houden stand.

## Taak 4 - De sandboxtests: GROEN, na een eigen meetfout

Eindstand:

```
uv run pytest -m sandbox -q -o addopts=""
2 failed, 60 passed, 9027 deselected, 1 xfailed in 3704.52s (1:01:44)
```

**De standaard sandboxset is 55/55 groen.** Beide falers zitten in
`test_sandbox_reallife.py`, en die suite valt buiten deze doorloop: het plan zet hem
expliciet buiten scope, en de eigen taak van het project draait
`-m "e2e and sandbox and not reallife"` (Taskfile r. 2050). Plain `-m sandbox` sluit hem
niet uit - een marker doet dat niet vanzelf - dus deze run pakte er acht extra tests bij
(63 in plaats van 55). Dat is meer dekking, geen minder; hieronder staat wat de twee
falers waren.

### Faler 1 - een verouderde test, geen fout in de code

`test_ui_env_vars_while_api_patches_same_file` zet omgevingsvariabelen via de UI op
**componentniveau**, controleert dat ze versleuteld in git staan (dat lukt), en zoekt de
naam daarna op het tabblad **Deployments**:

```
AssertionError: Env var 'RL_FROM_UI' not shown decrypted on the deployments tab of 'rl068-w81'
```

Dat is precies het gedrag dat drie commits met opzet hebben veranderd - `fc590e0a`
*"componentniveau naar Componenten, alleen overschrijvingen bij Deployments"*, gevolgd door
`804c226e` en `77f1a9a0`. Variabelen op componentniveau horen op **Componenten**;
Deployments toont alleen wat daar anders is dan de standaard. In de handmatige doorloop is
dat ook zo gezien: na het zetten van `DOORLOOP_RC110` en `LOGNIVEAU` staan die namen in de
kaart van het component.

Het commentaar in de test verwees bovendien naar `bg/_env-vars.html.j2` "onder
`active_tab == 'deployments'`", en dat sjabloon wordt sinds diezelfde commits **nergens meer
ingevoegd**: de variabelen worden nu direct in `bg/project-tabs.html.j2` (componentniveau)
en `bg/_section-deployments.html.j2` (overschrijvingen) gerenderd. Het bestand is daarmee
dood; niet verwijderd in deze PR, wel gemeld.

De test wijst nu naar het tabblad Componenten. **Niet opnieuw met de hele reallife-suite
nagemeten** - die kost een uur op het gedeelde cluster en valt buiten deze opdracht - maar
de aanname erachter is met de hand gecontroleerd op precies dat scherm.

### Faler 2 - het netwerk, niet de code

`test_add_deployment_rolls_out_whole_project` viel om op een image-pull:

```
gamma: image ophalen mislukt (ErrImagePull): failed to pull and unpack image
"ghcr.io/minbzk/base-images/e2e-allservices:latest": failed to do request:
Head "https://ghcr.io/v2/.../manifests/latest": EOF
```

Hetzelfde image is in deze run tientallen keren wél binnengehaald. Een `EOF` op de
manifest-HEAD is ghcr.io die de verbinding sluit, niet iets wat deze tak doet. Wel netjes
om te zien dat de foutmelding het component noemt en zegt wat er misging - dat is precies
wat een taak hoort te doen als hij faalt.

### De eerste run was rood, en het lag aan mij

De eerste poging leverde **8 failed en 46 errors in 6m31** op - bijna de hele suite - en dat
zag eruit als een kapotte tak. Twee dingen wezen de andere kant op:

- vrijwel elke test faalde in **precies 11,2 seconden**. Zoveel verschillende tests die
  allemaal even lang doen over hun eigen mislukking, dat is één oorzaak en geen 54;
- de suite deed er 6 minuten over waar hij normaal drie kwartier draait.

De oorzaak: ik gaf `E2E_SECRET_KEY` mee uit
`kubectl get cm operations-manager-config -o jsonpath='{.data.SECRET_KEY}'`, en die
configmap heeft **geen** sleutel `SECRET_KEY`. Hij heeft er precies één, `.env`, met het
hele bestand als waarde. `jsonpath` op een niet-bestaande sleutel geeft geen fout maar een
**lege string**, dus de suite ondertekende al haar sessiecookies met een lege sleutel; het
portaal wees ze af en elke test liep tegen de inlogpagina aan.

De goede aanroep is:

```bash
kubectl -n rig-system get cm operations-manager-config -o jsonpath='{.data.\.env}' \
  | grep -E '^SECRET_KEY=' | cut -d= -f2-
```

Opgeschreven omdat het precies het soort rood is waar deze doorloop voor waarschuwt: het
ziet eruit als een gebroken applicatie, het is een gebroken meetopstelling. Met de echte
sleutel is de suite groen.

## Taak 5 - De handmatige doorloop

Uitgevoerd met `scripts/doorloop_rc108.py` (aanmaken via de wizard, wachten tot ArgoCD
`Healthy` meldt, elk tabblad vastleggen) en daarna met de hand voor de stappen die RC-108
openliet. De plaatjes staan in `docs/doorloop-rc110/`.

### De tabbladen

Alle negen tabbladen tonen wat er hoort te staan, en **geen kop zonder inhoud**: waar niets
is staat dat er ook ("Geen backup schema ingesteld", "Nog geen metingen"). Dat is hetzelfde
beeld als RC-108, met de tabbalk van negen tabbladen en de voettekst met de commit.

### Componenten: omgevingsvariabelen en aliassen

Dit punt kon RC-108 niet bevestigen, omdat dat verse project geen aliassen had. Nu wel
gezet, via de bewerkdialoog van het component:

- de twee velden zijn CodeMirror-editors met een ENV/YAML-schakelaar; de `<textarea>`
  eronder staat op `display: none` en is de drager, niet het invoerveld;
- na opslaan staan `POSTGRES_HOST`, `OIDC_ISSUER`, `DOORLOOP_RC110` en `LOGNIVEAU`
  **in de kaart van het component** (`03-componenten.png` toont de kaart, de waarden zijn
  daarna toegevoegd en nagemeten op de pagina);
- de hulpknop bij het aliassenveld opent een dialoog met **de variabelen per dienst**:
  Keycloak Authentication, MinIO Object Storage, Namespace PostgreSQL Database, Namespace
  Redis Cache, Permanente opslag, Platform, Prometheus Metrics Scraper, Publiceren op het
  web, Tijdelijke schijfruimte (`12-aliassen-hulp.png`).

Eén kanttekening bij die hulpdialoog: hij somt **alle** diensten van het platform op,
terwijl de zin erboven zegt "Je kunt alleen variabelen gebruiken van diensten die dit
component ook echt gebruikt." Het component in kwestie gebruikt Keycloak, PostgreSQL,
publish-on-web en bijlagen - MinIO en Redis staan er dus bij zonder dat je ze mag gebruiken.
De lijst is een naslagwerk en de zin een waarschuwing, dus het is niet fout; het leest
alleen alsof de lijst gefilterd is.

### Het projectbestand na de bewerking

Nagelezen in `zad-projects` na het opslaan van het component:

- de aliassen staan als **één AGE-blok** (`aliases: |-` met precies één
  `BEGIN AGE ENCRYPTED FILE`), en de eigen omgevingsvariabelen net zo;
- het **realm-wachtwoord staat er nog**, evenals de rest van het `config`-blok. De bewerking
  wist niets van wat OPI zelf beheert.

### Bijlagen: een bestaande bijlage vervangen (RC-109)

Zowel via de API als via de UI gedaan, op een project waar het component `web` de bijlage
`cacert` als bestand gemount krijgt op `/etc/ssl/certs/ca-extra.pem`.

| Stap | Uitkomst |
|---|---|
| `POST .../attachments/attachment` (ca-v1.pem) | `{"attachment":"cacert","replaced":false}` |
| Koppelen aan component (`reference=cacert`) | koppeling in het projectbestand |
| `PUT .../attachments/attachment/cacert` (ca-v2.pem) | `{"attachment":"cacert","replaced":true}` |
| UI: knop **Vervangen** -> bestand kiezen -> Opslaan (ca-v3.pem) | `filename: ca-v3.pem` in de catalogus |
| Koppeling van het component na het vervangen | **ongewijzigd aanwezig** |

De dialoog (`11-bijlage-vervangen.png`) doet wat RC-109 belooft: de identifier ligt vast en
staat grijs, en de tekst zegt wat er gebeurt - *"De identifier blijft hetzelfde, dus alle
componenten die deze bijlage gebruiken blijven eraan gekoppeld. De vorige inhoud is daarna
weg."* De bijlagenkaart toont daarna de **nieuwe** bestandsnaam.

Twee dingen die het opschrijven waard zijn voor wie de API gebruikt:

- een bestaande bijlage aan een component koppelen gaat met `reference=<id>` op de
  component-POST; met alleen `attachment_id` krijg je
  `Geef een 'file' (nieuwe inhoud) of een 'reference' (bestaande bijlage)`, en met een
  `file` erbij `Bijlage 'cacert' bestaat al in project`. De foutmeldingen wijzen de weg,
  maar de POST doet twee dingen tegelijk;
- `provide-as: file` eist een `path`, en zegt dat ook.

### De review-stap noemt de aliassen niet

Opslaan in de componentdialoog gaat via een tussenstap **"Controleer je wijzigingen"** met
een knop *Bevestigen en verwerken*. Die samenvatting noemt naam, image, CPU, geheugen,
poorten, diensten, paden, opslag en bijlagen - maar **niet** de aliassen en niet de eigen
omgevingsvariabelen, ook niet als je ze net hebt ingevuld. Je bevestigt dus een wijziging
die je op dat scherm niet kunt zien. De waarden worden wel correct opgeslagen. Geen
blokkade, wel een gat in een scherm dat er juist voor bedoeld is.

### Backup en terugzetten (openstaand punt uit RC-108)

Met een merk in de database, zodat er iets te bewijzen viel:

1. `CREATE TABLE doorloop_rc110` met een rij `voor-de-backup`;
2. `POST /api/v1/backup/project/{p}/deployment/productie` -> `success`, 1 database, 10,4s;
3. de tabel gedropt;
4. `POST /api/v1/restore/project/{p}/deployment/productie/run/{run}` -> `success`;
5. het merk staat terug - in de **nieuwe generatie**: database `e2e62_glv_productie_v1`,
   schema `e2e62_glv_productie_v1`, en `generation: 1` in het projectbestand. De oude
   database is leeg gebleven.

Het terugzetten zelf klopt dus. Wat erna gebeurt niet - zie de volgende paragraaf.

### BLOKKEREND: na een restore is het project onbruikbaar

Twee keer gereproduceerd, op twee projecten, waarvan één (`e2e62-65f`) verder nergens voor
gebruikt was.

De restore **roteert het wachtwoord van de databasegebruiker**:

```
01:28:34,261  Password updated for user e2e62_glv_productie
01:28:34,535  Database e2e62_glv_productie_v1 created successfully
```

Daarna start hij de reparatie die de manifesten en het geheim zou moeten bijwerken - en die
faalt op precies dat nieuwe wachtwoord:

```
01:28:47,236  Triggering project refresh for e2e62-glv
01:28:48,765  ERROR  Error processing project: Database secret exists for e2e62-glv/productie
              but credentials are invalid. Manual intervention required to fix database user
              or update secret.
01:28:48,770  WARNING  Skipping ArgoCD sync due to critical failures
```

Het gevolg, nagemeten:

| Wat | Stand na de restore |
|---|---|
| Antwoord van de API | `{"status":"success", ..., "refresh_triggered": true}` |
| Geheim in de namespace | onveranderd sinds het aanmaken (`resourceVersion` gelijk) |
| `DATABASE_DB` in dat geheim | `e2e62_glv_productie` - de **oude**, nu lege database |
| `DATABASE_PASSWORD` in dat geheim | `psql`: `password authentication failed` |
| Nieuwe manifesten in `zad-deployments` | geen; laatste commit dateert van vóór de restore |
| ArgoCD | `Synced` / `Healthy` - hij ziet niets veranderen, dus er is niets te melden |

En het blijft niet bij dat ene geheim: **elke volgende wijziging op het project faalt**.
Een `POST .../services` erna kwam terug met
`Failed: Database secret exists for ... but credentials are invalid`, met de subtaken
"Project verwerken" en "Diensten en manifesten bijwerken" op `failed`. Het project is
daarmee op slot tot iemand met de hand ingrijpt.

Dat de API `success` meldt is het venijnige deel: de restore is inhoudelijk geslaagd (het
merk staat terug) en de melding is dus niet gelogen, maar wie erop afgaat heeft een project
dat niet meer werkt en dat ook niet meer bij te werken is.

**Niet hier gerepareerd, met opzet.** Wie het geheim mag bijwerken is een ontwerpkeuze: de
melding *Manual intervention required* staat er om te voorkomen dat OPI een geheim
overschrijft dat iemand anders beheert. De juiste reparatie (de restore werkt het geheim
zelf bij, óf de refresh accepteert een wachtwoord dat de restore net zelf heeft gezet) hoort
met een eigen test in een eigen taak. De hele weg is nieuw ten opzichte van main
(`33f6fd0c feat(restore): terugzetten in de eigen database of bucket zonder doelvelden`),
dus dit is geen bestaand gedrag dat we al hadden.

### Verwijderen: schoon, en het RC-108-lek is dicht

Twee keer gedaan: één project via de UI (met de bevestigingsdialoog) en één via de API.

| Wat | Na het verwijderen |
|---|---|
| Projectbestand in `zad-projects` | weg (404) |
| Namespace | weg |
| Database | weg (`pg_database` kent hem niet meer) |
| Bucket in MinIO | weg (project met `minio-storage`, bucket `rc110-n01-productie`) |
| Keycloak-realm | weg (404, terwijl een levend project 200 geeft) |
| ArgoCD-applicatie | weg |
| Map in `zad-argo-user-applications` | **weg** |

Die laatste regel is de bevinding van RC-108, en die is **niet meer te reproduceren**: de
map van het verwijderde project is netjes opgeruimd. Wat er nog wél staat, is de erfenis van
eerdere rondes: `enval-a6a`, `enval-box`, `enval-m4k`, `enval-xhr`, `invit-3jf`, `invit-au4`,
`invit-eux`, `invit-ftl`, `invit-knd`, `pgsch-at8`. Dat zijn wezen uit RC-108 en eerder, geen
nieuwe. Ze opruimen is een taak op zich; het lek zelf is gedicht.

Ook goed om te weten: een project dat door de restore op slot zit (zie hierboven) laat zich
nog steeds verwijderen.

### GEREPAREERD: de kaart Pods stond altijd op 0

Gevonden door naar het dashboard te kijken: drie projecten met elk een draaiende pod, en de
kaart **Pods** meldde 0. Prometheus wist het wel - `count(kube_pod_info)` gaf 66 - dus het
lag niet aan de meting.

De oorzaak zit in de opsplitsing van het dashboard. Sinds `2b70b13b` wordt het
resourcegebruik apart opgehaald (`/dashboard/resource-usage`), want die Prometheus-queries
duurden te lang om de pagina op te laten wachten. Het aantal pods komt uit diezelfde
queries, maar de kaart bleef in de **pagina** staan - die rendert met de vaste
`pod_count = 0` uit de route - terwijl het fragment het echte getal weggooide (`_pods`).

Het fragment schuift de kaart er nu out-of-band overheen, en wel **buiten** de voorwaarde
die op een afwezige Prometheus test, zodat er ook dan een getal komt in plaats van een kaart
die blijft hangen. Nagemeten op het scherm: met één draaiend project staat er nu 1
(`10-dashboard-gebruik-per-project.png`). Test: `tests/test_dashboard_pods_kerncijfer.py`.

### Dashboard: Gebruik per project (RC-107)

Op het scherm gecontroleerd: **Gebruik per project** staat onder Resourcegebruik, met per
project **geheugen boven CPU**, gesorteerd op geheugen (8 MiB, 7 MiB, 7 MiB in de meting met
drie projecten), en de projectnamen zijn links die naar de projectpagina gaan.

## Taak 6 - De API-weg

Volledig doorlopen met een SSO-bearer; het protocol staat in
`docs/doorloop-rc110/api-doorloop.log`.

| Stap | Uitkomst |
|---|---|
| `POST /api/v2/projects` (Bearer) | 202, taak `completed` |
| `POST .../services` (`publish-on-web`, `minio-storage`) | 202 + 202, beide `completed` |
| `POST .../components` | 202, `completed` |
| `POST .../:upsert-deployment` | 202, `completed` |
| `GET .../{project}`, `.../deployments`, `.../deployments/productie` | 200 |
| `DELETE /api/projects/{project}` | `completed`, alles opgeruimd (zie hierboven) |

**Een aanwijzing die in RC-108 anders staat.** Het verslag van RC-108 zegt dat de client
`zad-cli` in realm `rig-platform` zit. Dat is hier niet zo: `rig-platform` antwoordt
`Client not found`, en de client staat in realm **`operations-manager`** (waar
`opi/configs/keycloak/operations-manager-realm.yaml` hem ook definieert). Verder klopt de
beschrijving: publieke client, authorization code + PKCE, loopback-redirect, en de `aud` is
`["zad-api", "account"]`.

### De twee punten van de zad-cli

**1. `deployment describe` draagt `source` en `pending_rollout`.** Gemeten:

- `GET .../deployments` (de lijst): `pending_rollout` is `null` bij het item;
- `GET .../deployments/productie` (het enkele antwoord): `source: "project-file"` en
  `pending_rollout: {"project": ..., "count": 1, "since": ..., "task_types": ["create_project"]}`.

Precies de belofte: leeg in de lijst, gevuld op het enkele antwoord.

**2. Een restore met een onbekende referentie noemt namen die `backup list` ook teruggeeft.**
Gemeten op een project met een bucketbackup:

```
POST /api/v1/restore/bucket/sandboxed-local/rig-rc110-n01/bestaat-niet
404  No deployment of project 'rc110-n01' has a bucket backup named 'bestaat-niet'.
     This project has: 'productie-minio'. Supply the target_minio_endpoint, ...
```

En `GET /api/v1/backup/runs/rc110-n01/productie` geeft als `reference_name`: `productie-minio`.
Dezelfde naam, dus je kunt de foutmelding rechtstreeks overtypen. De databasevariant doet
hetzelfde en noemt `productie-postgresql`, `productie-namespace`, `productie-database`.

## Wat er misging tijdens de doorloop zelf

Eerlijkheidshalve, want het kostte tijd en het staat de volgende keer weer klaar:

- **`--opruimen` in `scripts/doorloop_rc108.py` doet niets.** De vlag wordt gedeclareerd en
  nooit gelezen; twee metingen die ik met `--opruimen` draaide lieten hun project gewoon
  staan. Dat verklaarde ook waarom meting 2 en 3 op een steeds voller cluster liepen -
  wat achteraf juist de nuttigste meting opleverde, maar dat was geluk, geen opzet.
- **Een bestand `token.py` in de werkmap** legde elke Python-start om: `tokenize` importeert
  `token`, en dan valt `logging` om met `partially initialized module`. De foutmelding wijst
  naar `concurrent.futures` en niet naar het eigen bestand.
- **De aliassenvelden zijn niet met `fill()` te vullen.** De `<textarea>` staat op
  `display: none` omdat CodeMirror hem vervangt; typen in `.cm-content` werkt wel.
- **`Opslaan` in de componentdialoog is niet de laatste stap** - er komt een review-stap
  achter. Een eerste poging leek daardoor stilzwijgend te falen terwijl er gewoon nog een
  knop wachtte.

## Aanbevelingen

1. **De restore-weg repareren** (blokkerend, zie boven). Eigen taak, eigen test: een restore
   gevolgd door een willekeurige tweede wijziging op hetzelfde project moet slagen.
2. **Het ritme van de gezondheidsprobe** bewust kiezen en de commentaarregel bij
   `HEALTH_CHECK_INTERVAL_SECONDS` laten kloppen.
3. **De review-stap van de componentdialoog** de aliassen en omgevingsvariabelen laten tonen.
4. **De wezen in `zad-argo-user-applications`** uit eerdere rondes opruimen.
5. **Een schone sandbox vanaf nul** op een machine met de sleutels; twee rondes op rij
   onbewezen is genoeg.
6. **`bg/_env-vars.html.j2` opruimen**: het sjabloon wordt nergens meer ingevoegd sinds de
   variabelen naar Componenten verhuisden. Hier gemeld, niet verwijderd.
