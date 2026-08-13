# Tweede generale repetitie: dezelfde keten, nu met de klok erbij

Datum: 13 augustus 2026. Taak: RC-89. Uitgevoerd op de gedeelde sandbox op de dev-server,
op commit `52a7f330` van de tak `de-tweede-generale-repetitie-en-deze-keer-met-de-k`.

Dit is de herhaling van `docs/generale-repetitie-2026-08-12.md`, die adviseerde nog niet
uit te rollen. Wat daar al is aangetoond is hier opnieuw gedraaid, niet opnieuw beredeneerd.
Het plan staat in `plans/de-tweede-generale-repetitie.md`.

De harde eis van deze doorloop was de tijd. Vandaar dat de duur per stap vooropstaat en niet
in een voetnoot.

## Uitgangspositie

| Wat | Uitkomst |
|---|---|
| Draaiende versie | `GET /version` -> `52a7f330`, tak `de-tweede-generale-repetitie-en-deze-keer-met-de-k`, image `operations-manager:rc-89-` |
| Sleutel | `SECRET_KEY` uit configmap `operations-manager-config` (`/etc/config/.env`), `CLUSTER_MANAGER=sandboxed-local` |
| Domein | `*.sandbox.rijksapp.dev` -> `127.0.0.1`, bevestigd |
| Aangetroffen projecttoestand | 1 projectbestand, maar 24 ArgoCD-applicaties en 23 AppProjects; 22 verweesde projectmappen in de argo-repo (bevinding 5) |

**Afwijking van het plan, dezelfde als vorige keer.** Het plan schrijft `task sandbox:setup`
voor. Dat is hier NIET gedraaid: `workflow/sandbox.md` verbiedt het expliciet in een sessie
op de dev-server, want die taak hoort bij een volledige lokale dev-opzet en rolt een
registry-image uit in plaats van de build van deze tak. In plaats daarvan is via
`sandbox-deploy` de eigen build uitgerold en is de projecttoestand schoongemaakt. De
cluster-infrastructuur zelf is niet opnieuw opgebouwd.

Om dezelfde reden is `security/sandbox-key.txt` in deze sessie niet aanwezig: de AGE-sleutel
van de sandbox leeft in het cluster (secret `sops-age-key` in `rig-system`), en dat is de
sleutel die OPI werkelijk gebruikt. De controle die het plan vraagt is dus op het cluster
gedaan en niet op de werkkopie.

## De tijd, eerst

Totale wandkloktijd van de doorloop: **1 uur 25**, waarvan de beide e2e-suites samen ruim een
uur. Alles behalve die suites - de uitrol, het opruimen van de aangetroffen rommel, alle zes
stappen, de nieuwe toets en de metingen - kostte samen **circa 20 minuten**. De vorige doorloop
kostte bijna acht uur over meerdere sessies. Het verschil zit niet in ander werk maar in ander
wachten: overal is op de voorwaarde gewacht (`wait_for_task()`, `kubectl rollout status`, pollen
op het projectbestand) en nergens op de klok.

| Stap | Duur | Waar de tijd zat |
|---|---:|---|
| Uitrollen eigen build (`sandbox-deploy`) | 69 s | image-build uit cache, `kind load`, rollout |
| Opruimen van de aangetroffen sandbox | ~7 min | 22 verweesde projectmappen + 24 applicaties; ArgoCD-refresh en finalizers |
| 1. Conversie van 47 productiebestanden | 6 s | rekenwerk, geen wachten |
| 2. Aanmaken via de wizard (4 diensten) | 67 s | provisioning + ArgoCD tot `Healthy` |
| 3a. Aanmaken via de API | 0,1 s | het projectbestand zelf |
| 3b. Deployment erbij via de API | 15 s | namespace + manifesten |
| 3c. Component met impliciete dienstselectie | 27 s | database aanmaken + uitrol |
| 3d. Afgewezen component (de nieuwe toets) | 2 s | weigering, geen werk |
| 4a. Tweede deployment | 58 s | tweede database, ingress, uitrol |
| 4b. TLS-override via de deploymentmodal | 52 s | opslaan + herverwerken + uitrol |
| 5a. Backup (1 PVC + 1 database) | 36-42 s | twee backup-pods, elk ~10-21 s |
| 5b. Terugzetten zonder doelvelden | 11 s | restore-pod |
| 5c. Half ingevulde doelvelden | < 1 s | 422 uit het model, geen pod |
| 5d. Bestemming die niet resolvet | 11 s | restore-pod tot de bestemmingscontrole |
| 6. Reprocess van beide deployments | 13 s | herverwerken + ArgoCD-sync |
| Dubbele-`id`-meting in de browser | 40 s | zes pagina's laden |
| Opruimen van beide testprojecten | 31 s + 30 s | afbraak + ArgoCD |
| e2e lokaal (`-m "e2e and not sandbox"`) | 10 min 09 | 367 browsertests |
| e2e sandbox (`-m "e2e and sandbox"`) | 52 min 23 | echte provisioning per test |

**Aanmaken plus opruimen blijft ruim binnen het half uur.** Het wizardproject kostte
67 s aanmaken en 30 s opruimen: **1 minuut 37**. Het API-project kostte 43 s aanmaken
(project, deployment en component samen) en 31 s opruimen: **1 minuut 14**. Dat is
respectievelijk 5% en 4% van het budget.

**Wat het langst duurde, en waarom.** In volgorde:

1. **De sandbox-e2e-suite** (52 min, 60% van de hele doorloop). Dat is echt werk: de suite
   provisioneert per test een compleet project, en 46 geslaagde tests betekent tientallen
   volledige aanmaak- en opruimrondes. Hier valt tijd te winnen door projecten tussen tests te
   delen, niet door beter te wachten. Het is ook de post die de doorloop over de lease-grens
   duwt (bevinding 9).
2. **Het opruimen van de aangetroffen rommel** (~7 min). Dat is *geen* eigen werk maar een
   erfenis, en het is de duurste post die volledig te vermijden is: bevinding 5 hieronder is
   de oorzaak, en die repareren haalt deze post naar nul.
3. **De tweede deployment** (58 s) en **het aanmaken via de wizard** (67 s). Beide zijn
   provisioning met een ArgoCD-sync erachter; dat is de ondergrens van de keten zelf.

**Waar verbeteren het meeste oplevert:** twee dingen, in deze volgorde.

1. **De sandbox-suite korter maken door projecten te delen.** Hij is 60% van de doorloop, en
   hij is de reden dat een doorloop niet in één lease past. Elke module die nu zijn eigen
   project aanmaakt betaalt daar ruim een minuut voor.
2. **Bevinding 5 repareren.** Niet omdat hij de traagste stap raakt (~7 min), maar omdat hij de
   enige is die *terugkerend* tijd kost aan iedereen die na jou de sandbox gebruikt, en omdat de
   opruiming die hij nodig maakt met de hand gebeurt.

Wat níet loont is beter wachten: de zes stappen samen kosten vier en een halve minuut, en dat is
provisioning die echt gebeurt.

## Per stap

### 1. Conversie van bestaande projectbestanden - GESLAAGD

Gedraaid op dezelfde 47 echte productiebestanden als vorige keer, met de bestaande laag-1
test:

```
RIG_PROJECTS_DIR=<klon>/projects uv run pytest tests/test_upgrade_safety_replay.py -q
-> 9 passed in 3.94s
```

Uitgesplitst, en identiek aan de vorige doorloop:

| Van | Aantal | Naar |
|---|---|---|
| 2.0 | 5 | 2.7 |
| 2.2 | 41 | 2.7 |
| 2.2 (geen migratie nodig) | 1 | 2.2 |

De dp-bn7-valkuil is opnieuw meetbaar: **46 van de 47 bestanden valideren NIET op de rauwe
gegevens** en wel na migratie. Na migratie mislukt er geen enkel bestand.

### 2. Project via de wizard, meerdere diensten - GESLAAGD

Project `r8w-c98`, aangemaakt via de echte wizard met vier diensten aan: `publish-on-web`,
`keycloak`, `postgresql-database` en `persistent-storage`. In **67 seconden** van het
openen van de wizard tot een applicatie die `Synced`/`Healthy` meldt.

De pod draait en meldt zijn eigen bindingen:

```
all_ok= True ready= True
  oidc: bound=True ok=True          postgres: bound=True ok=True
  platform: bound=True ok=True      storage-data: bound=True ok=True
  web: bound=True ok=True
```

Dat is de hele keten in één meting: elke gekozen dienst is niet alleen bijgeschreven maar ook
geprovisioneerd en vanuit de werklast bruikbaar.

### 3. Hetzelfde via de API, inclusief impliciete dienstselectie - GESLAAGD

Project `r8a-npu` aangemaakt met `POST /api/v2/projects`. Die route wil een SSO-bearer; het
token is opnieuw gehaald via auth-code + PKCE met de publieke client `zad-cli` (`aud`:
`["zad-api", "account"]`). `directAccessGrantsEnabled` staat uit op die client, dus een
wachtwoord-grant is geen sluiproute - de weg die de CLI loopt is de enige weg.

Impliciete dienstselectie (RC-84), in twee richtingen gemeten:

- **Mag wel.** Component `web` met `services: ["postgresql-database"]` op een project dat die
  dienst niet had. De dienst meldt zichzelf aan op projectniveau; het projectbestand toont hem
  daarna zowel op project- als op componentniveau, en de pod draait.
- **Mag niet.** Zie de volgende paragraaf: dat is nu een aparte toets geworden.

### De nieuwe toets: een afgewezen handeling zegt dat het misging - GESLAAGD

Dit was bevinding 5 van de vorige doorloop en de reden dat een client kon denken dat het goed
ging. Gemeten op dezelfde handeling als toen (een component dat een dienst vraagt die eerst op
projectniveau gekozen moet worden):

```
STATUS        = failed
result.status = failed
error_message = Services that must be enabled at project level first: ['publish-on-web'].
                They need project-level configuration that cannot be assumed, so they are
                not added automatically.
subtasks      = [('Component validatie', 'completed'), ('Component toevoegen', 'failed')]
```

Het veld waar een aanroeper als eerste op kijkt, `status`, zegt nu `failed`. Vorige keer stond
daar `completed` met de fout eronder verstopt. De weigering zelf is ongewijzigd en noemt nog
steeds wat er moet gebeuren.

Er staat nu ook een vangrail op de LEVENDE weg: `tests/e2e/test_sandbox_repetitie.py`. De
beslissing zelf was al gedekt door `tests/test_task_status_reports_failure.py`, maar dat toetst
de worker met nagemaakte handlers; wat daar niet in zat is de weg die een aanroeper werkelijk
loopt.

### 4. Een tweede deployment, met de TLS-override - GESLAAGD, met een kanttekening

`productie` en `staging` draaien naast elkaar op `r8w-c98`, elk met een eigen database
(`r8w_c98_productie` / `r8w_c98_staging`), een eigen ingress en een eigen hostnaam.

De TLS-override per deployment-component (RC-78), die de vorige doorloop expliciet niet heeft
getoetst, is hier wel uitgeoefend, en wel via de modal waar hij voor gebouwd is
(`modal-edit-deployment-1`). Gemeten op drie plekken achter elkaar:

1. **De modal biedt hem aan** op de deployment-component-laag, met vier keuzes waarvan de lege
   de erfenis benoemt: `"" = Erven van het component: Standaard certificaat (platform regelt
   het)`, plus `standard`, `passthrough` en `provided`. Dat is precies wat het plan van RC-78
   eiste ("laat zien wat het component zegt").
2. **Het projectbestand bewaart hem op de juiste laag.** Het component houdt `tls: standard`;
   alleen `staging` krijgt de override, op het pad van de deployment-component-laag:

   ```yaml
   components:
     - name: web
       services: [{reference: publish-on-web, config: {tls: standard}}, ...]
   deployments:
     - name: productie
       components: [{reference: web}]                       # geen override: erft
     - name: staging
       components: [{reference: web, services: {publish-on-web: {config: {tls: passthrough}}}}]
   ```

3. **Het cluster rendert er een ander ingress-object van.** `staging-web` draagt
   `nginx.ingress.kubernetes.io/ssl-passthrough: "true"`, `productie-web` niet. Eén component,
   twee deployments, twee verschillende ingressen - wat de override belooft.

De kanttekening is bevinding 4: op dit cluster verandert die annotatie het *geserveerde*
certificaat niet.

`passthrough` en niet `provided`, omdat `provided` een certificaat-bijlage vereist en het
model `provided` zonder bijlage terecht weigert. Dat de override `provided` ook kan uitzetten
is al gedekt door `tests/test_deployment_certificate_override.py`.

### 5. Backup en terugzetten - GESLAAGD

Met echte gegevens:

1. Tabel `repetitie89` met een rij aangemaakt in het schema `r8w_c98_productie`.
2. `POST /api/v1/backup/project/r8w-c98/deployment/productie` -> 1 PVC en 1 database.
3. Tabel weggegooid.
4. Terugzetten met een leeg lichaam -> `success`, in de eigen database van het project.
5. **De rij staat er weer.** Het terugzetten draagt dus werkelijk gegevens over.

De drie gevallen uit het plan:

| Geval | Uitkomst |
|---|---|
| Zonder doelvelden | 200, teruggezet in de eigen dienst van het project (RC-81) |
| Half ingevuld (3 van de 4) | 422: *"Specify all target fields or none of them ... Missing: target_database_password"* - noemt het ontbrekende veld |
| Bestemming die niet resolvet | **400 met `error_category: InvalidTarget`** (RC-82), en de pod-log noemt de oorzaak: *"could not translate host name"* |

Eén observatie over de methode, want hij kan een volgende meting misleiden: een tabel die als
`postgres` in het schema `public` wordt gezet komt **niet** terug, en het terugzetten meldt dan
toch `success`. Dat is verdedigbaar - er is teruggezet wat er geback-upt is, en de dump loopt
als de projectgebruiker over het schema van het project - maar wie de eerste meting in `public`
doet, meet niets en denkt dat het terugzetten stuk is. Dat overkwam deze doorloop ook.

### 6. Reprocess van een bestaand project - GESLAAGD

`POST /api/v2/projects/r8w-c98/:refresh` verwerkt beide deployments opnieuw en eindigt op
`completed` in 13 seconden, met elke stap in het Nederlands en met zijn onderwerp in een eigen
veld (RC-83):

```
Database klaarmaken        subject=productie      Database klaarmaken        subject=staging
Keycloak-SSO klaarmaken    subject=productie      Keycloak-SSO klaarmaken    subject=staging
Wachten tot 2 applicatie(s) gesynchroniseerd zijn -> completed
productie: uitgerold en gezond      staging: uitgerold en gezond
```

Geen stille mislukking.

## De e2e-suites

| Suite | Uitkomst |
|---|---|
| Lokaal (`-m "e2e and not sandbox"`) | **1 gefaald, 362 geslaagd, 1 overgeslagen, 3 xfailed** in 10m09 |
| Sandbox (`-m "e2e and sandbox"`) | **3 gefaald, 46 geslaagd, 1 xfailed** in 52m23 |

De lokale suite is daarmee van **7 gefaald naar 1**, en die ene is de bekende paginamarge uit
bevinding 1. De zes andere fouten van de vorige doorloop (de dubbele `id`'s en de vier
achterhaalde tests over het verdwenen kopieerknopje) zijn weg.

De sandbox-suite is van **9 gefaald / 16 geslaagd / 25 fouten bij het opzetten** naar
**3 gefaald / 46 geslaagd / 0 fouten bij het opzetten**. Dat de opzetfouten weg zijn is het
belangrijkste getal van de twee: vorige keer kwamen 25 tests niet eens aan hun eigen toets toe.

De drie die rood zijn staan alle drie in `test_sandbox_reallife.py`, en ze zijn **niet aan deze
tak toe te schrijven**. Zie bevinding 9.

## Bevindingen

### 1. Bekend en verwacht: paginamarge

`test_lotc_paginamarge.py::test_de_kolom_wordt_niet_eindeloos_breed` bewaakt een kolombreedte
onder 1400 terwijl de gekozen bovengrens 1440 oplevert. Dat is een getal dat de eigenaar moet
kiezen, geen fout. Zoals het plan voorschrijft: genoemd, niet gerepareerd.

### 2. Opgelost: geen dubbele `id`'s meer, gemeten in de browser

De zwaarste openstaande blokkade van de vorige doorloop raakte *elk* aanvinkvakje in de
applicatie. Nagemeten op de draaiende sandbox, door in de lichte boom alle `id`'s te tellen en
op dubbelen te controleren:

| Pagina | `id`'s | aanvinkvakjes | dubbel |
|---|---:|---:|---|
| `/` | 17 | 0 | geen |
| `/projects` | 23 | 0 | geen |
| `/projects/details/r8w-c98` | 53 | 0 | geen |
| `/services` | 16 | 0 | geen |
| `modal-edit-keycloak-config` | 68 | 1 | geen |

De laatste regel is de beslissende: dat is de modal met het aanvinkvakje waarop bevinding 2
destijds gemeten werd (`.../restrict-access/enabled`), en waar toen twee elementen met
hetzelfde `id` stonden. Nu één.

### 3. Opgelost: een mislukte subtaak meldt zich in `status`

Zie stap "de nieuwe toets" hierboven. Bevinding 5 van de vorige doorloop is dicht.

### 4. De TLS-override komt tot in het ingress, maar `passthrough` heeft geen waarneembaar effect

De override doet alles wat van hem gevraagd is tot en met het ingress-object (zie stap 4). Wat
er daarna *niet* gebeurt, is dat de ingress-controller zich anders gaat gedragen:

- beide hostnamen serveren nog het platformcertificaat (`CN=*.sandbox.rijksapp.dev`, Let's
  Encrypt), gemeten met `openssl s_client -servername`;
- de werklast van `staging` blijft gewoon over HTTPS bereikbaar, terwijl passthrough betekent
  dat de pod zijn eigen certificaat moet presenteren - en deze pod praat alleen platte HTTP;
- in de gegenereerde `nginx.conf` staat `web-staging-...` als een gewoon server-blok en niet in
  de passthrough-map.

De controller heeft `--enable-ssl-passthrough` wel degelijk aan staan, dus dat is het niet. De
waarschijnlijke oorzaak is te zien in het ingress zelf: `spec.tls` is `[{}]` - één lege regel
zonder `hosts`. nginx-ingress neemt een host pas in zijn passthrough-map op als die host in een
TLS-blok staat.

**Niet gerepareerd.** Of `passthrough` een eigen `spec.tls`-blok met hostnaam hoort te krijgen
is een besluit over het ingress-sjabloon met gevolgen voor `standard` en `provided`, en dat is
geen tikfout. Wat wel vaststaat: de override *per deployment* werkt, en `standard` en
`provided` gaan langs een ander pad dan de annotatie die hier blijft liggen.

### 5. OPI's eigen delete laat een AppProject en een repo-secret achter - en dat stapelt op

Dit is bevinding 8 van de vorige doorloop, en hij is niet klein gebleken: hij is de motor
achter de rommel die deze doorloop bij aanvang aantrof.

**Wat blijft er staan.** Na een geslaagde `DELETE /api/projects/r8w-c98` (die de applicaties,
de namespace, de projectmap in de argo-repo en het projectbestand netjes weghaalt) staan er in
`rig-system` nog twee objecten:

```
Secret     r8w-c98-main-repo    OutOfSync
AppProject r8w-c98-r8w-c98      OutOfSync
```

Dat zijn precies de twee resources die de root-applicatie `user-applications` daarna op
`OutOfSync` zetten, en ze verdwijnen niet vanzelf.

**Waarom dat oploopt.** Bij aanvang stond er in `zad-projects` nog één projectbestand, maar in
het cluster stonden **24 applicaties en 23 AppProjects**, en in `zad-argo-user-applications`
**22 projectmappen** zonder bijbehorend projectbestand. Van één daarvan (`pgsch-1sj`) is in de
git-historie te zien dat hij netjes via OPI verwijderd is - er staat een commit *"Delete project
'pgsch-1sj' - removed project file after deployment cleanup"* - terwijl er geen bijbehorende
*"Delete ArgoCD resources"*-commit tegenover staat. Bij andere projecten (`nsv99-add`,
`enval-4mu`) staat die commit er wel. De opruiming is dus niet consequent, en elke keer dat hij
overslaat blijft er een map staan die ArgoCD de applicatie laat herrijzen.

**En het is deze doorloop opnieuw gebeurd, meetbaar.** Na afloop van de sandbox-suite stonden er
nog twee projectbestanden (opgeruimd) en daarna **vier verweesde projectmappen** in
`zad-argo-user-applications` - `invit-zyf`, `pgsch-gt5`, `rl155-3gf` en `sleep-ngm` - elk met een
ArgoCD-applicatie en een AppProject die eruit herrezen, terwijl hun projectbestand netjes weg
was. Vier projecten uit één suite-run. Dat is de aangroei van de begintoestand hierboven, van
dichtbij gezien.

Een plausibel mechanisme staat in `GitConnector.ensure_repo_cloned()`: die haalt per sessie
**eenmalig** nieuwe commits op (`_fetched_in_session`). Een werkkopie die daarna verouderd
raakt laat `_delete_project_argocd_folder()` de map als `not_found` zien, waarna er niets te
committen valt en de opruiming stil overslaat. Dat past bij het patroon (het gaat mis bij
projecten die kort na elkaar verwijderd worden, en goed bij losse verwijderingen), maar het is
niet uit de logs bevestigd: de pod was al vervangen (bevinding 9).

**Niet gerepareerd.** De reparatie raakt of de opruimvolgorde of het fetch-gedrag van de
git-connector, en beide zijn een besluit. Wel opgeruimd: alle 22 mappen die deze doorloop
aantrof, plus alles wat de doorloop zelf achterliet. Eindstand van de sandbox: geen
projectbestanden, geen projectmappen in de argo-repo, alleen `sandbox-infrastructure` en
`user-applications` (beide `Synced`/`Healthy`), en alleen het AppProject `default`.

### 6. ArgoCD prunet niet als de sync *alles* zou wissen

Bijvangst van dat opruimen, en goed om te weten voor wie het nog eens doet: nadat de laatste
projectmap uit `zad-argo-user-applications` verdwenen was, bleef `user-applications` op
`OutOfSync` staan met:

```
Skipping sync attempt to [241905c2]: auto-sync will wipe out all resources
```

Dat is een veiligheidsklep van ArgoCD en geen fout van ons. Gevolg is wel dat wie de laatste
projectmap opruimt de root-applicatie klem zet tot er weer een project bijkomt; de verweesde
applicaties moeten dan met de hand weg. Vermeld omdat het bij het opruimen van bevinding 5
gegarandeerd langskomt.

### 7. Klein: de deploymentmodal toont "Omgevingsvariabelen" twee keer

Op `modal-edit-deployment-<n>` staat de kop **Omgevingsvariabelen** tweemaal onder elkaar, elk
met een eigen omschrijving ("Deployment-specifieke omgevingsvariabelen voor dit component." en
"Deployment-specifieke omgevingsvariabelen voor dit component. Overschrijf de
omgevingsvariabelen uit de componentdefinitie voor deze deployment."). Eén veld, twee koppen.
Zie de schermafbeelding in de bijlage. Kosmetisch, maar het staat in beeld bij elke deployment.

### 8. Klein: `sandbox-deploy` racet zijn eigen versiecontrole

De uitrol slaagde, maar de helper meldde daarna:

```
[sandbox-deploy] running /version : {"version":"5c026ecc", ...}
[sandbox-deploy] WARN - /version does not clearly show 52a7f330. Re-run after the pod is ready.
```

Een seconde later gaf `/version` wel degelijk `52a7f330`. De controle bevraagt `/version` zodra
`kubectl rollout status` terug is, terwijl het ingress-endpoint dan nog even de oude pod kan
bedienen. De waarschuwing is dus vals alarm, maar wel het soort vals alarm dat iemand een
tweede build laat draaien.

### 9. De sandbox-lease is korter dan een volledige sandbox-suite, en dat heeft deze meting geraakt

De drie rode tests in de sandbox-suite zijn `test_ui_edits_while_api_task_runs_on_same_file`,
`test_ui_env_vars_while_api_patches_same_file` en `test_final_state_of_all_projects` - de laatste
is cumulatief en meet de nasleep van de eerste twee. Ze falen omdat vier `update_image`-taken
(op `rl055-rvc`, `rl155-3gf`, `rl256-q2v` en `rl356-xsv`) eindigden op
*"Failed to update image: Failed to process deployment productie"*.

**Wat er onder lag is niet deze tak.** De tijden vallen samen tot op de seconde:

| Tijd (UTC) | Gebeurtenis |
|---|---|
| 06:24:56 | mijn build (`rc-89`) uitgerold, replicaset `c8875b7fd` |
| ~07:24 | mijn sandbox-lease van een uur verloopt (geclaimd om 06:24) |
| **07:28:42 / 07:28:43** | **twee nieuwe replicasets: een andere PR rolt zijn build uit over het cluster** |
| 07:28:45 - 07:28:46 | de vier `update_image`-taken worden aangemaakt |
| 07:31:06 - 07:31:23 | die taken beginnen pas (2,5 minuut later) en falen binnen 1-2 seconden |
| 07:36:25, 07:42:42 | nog twee uitrollen over hetzelfde cluster |

De OPI waar die taken op draaiden werd dus drie seconden vóór hun aanmaak vervangen, en daarna
nog twee keer. De logs van die pod zijn met de pod verdwenen, dus de precieze foutregel is niet
meer te achterhalen - maar een suite die halverwege drie keer onder zich vandaan wordt uitgerold
meet niets meer over de code die zij zou toetsen.

**Niemand heeft hier iets fout gedaan**, en dat is juist de bevinding: de lease duurt een uur, en
alleen de sandbox-suite al kost 52 minuten. Tel daar de uitrol, de zes stappen en het opruimen
bij op, en een volledige doorloop past per definitie niet in één lease. Het slot doet dan precies
wat het moet - de volgende PR mag erin - maar de lopende meting is stuk.

**Niet opnieuw gemeten.** Het cluster is inmiddels van een andere PR (`orch sandbox status`:
HELD by RC-91) en draait diens build. Dat mag niet weggenomen worden, dus deze drie blijven
staan als "rood door een verstoorde meting", niet als "rood".

Wat dit vraagt is een besluit, geen reparatie: of de lease meebeweegt met wat er draait, of een
doorloop de suite in stukken draait. Dat hoort bij degene die het slot beheert.

## Het oordeel

**De keten is gezond en de schil is dat nu ook.**

Alle zes stappen zijn geslaagd, plus de nieuwe toets. Elk productiebestand migreert en
valideert, wizard en API leveren allebei een draaiend project op, impliciete dienstselectie doet
wat hij belooft en weigert netjes wat hij niet mag, twee deployments draaien naast elkaar met
hun eigen database én met een verschillende certificaatafhandeling, backup en terugzetten dragen
echte gegevens over, een onbereikbare bestemming geeft de beloofde 400 met `InvalidTarget`, en
reprocess faalt niet stil.

De drie punten die vorige keer het uitrollen tegenhielden zijn alle drie weg:

| Blokkade van 12 augustus | Nu |
|---|---|
| Dubbele `id` op elk aanvinkvakje | weg, in de browser nagemeten op zes pagina's |
| Een mislukte subtaak meldde `completed` | `status` zegt `failed`, met vangrail op de levende weg |
| Wankele wizard-fixture sloopte de omgeving | 0 fouten bij het opzetten, tegen 25 vorige keer; 46 geslaagd tegen 16 |

**Advies: uitrollen kan.** Geen van de bevindingen hierboven is een blokkade:

- bevinding 1 is een getal dat de eigenaar moet kiezen;
- bevinding 4 raakt alleen `passthrough`, een modus die vandaag door geen enkel
  productieproject gebruikt wordt, en de override zelf werkt aantoonbaar per deployment;
- bevinding 5 is hygiëne in de opruiming: hij laat rommel achter, maar hij breekt geen draaiend
  project en de half kapotte toestand uit de vorige doorloop (weg uit het cluster, aanwezig in
  ZAD) treedt niet meer op;
- bevindingen 6, 7 en 8 zijn klein;
- bevinding 9 gaat over het meetgereedschap, niet over het product.

**Wat dit advies niet dekt.** De drie rode tests uit bevinding 9 zijn niet schoon nagemeten. De
aanwijzing dat ze door een vreemde uitrol omvielen is sterk - drie seconden verschil, en daarna
nog twee uitrollen - maar het is een aanwijzing en geen bewijs, want de logs van die pod zijn
weg. Wie dat hard wil hebben vóór de uitrol, laat `test_sandbox_reallife.py` alleen nog een keer
draaien op een cluster dat een uur met rust gelaten wordt; dat kost twintig minuten. Alle andere
uitspraken in dit verslag staan op eigen metingen die wél ongestoord zijn gedaan (alle zes
stappen en de nieuwe toets waren om 06:56 klaar, ruim binnen de lease).

Wat voorrang verdient na de uitrol is **bevinding 5**: hij kost bij elke doorloop opnieuw
handwerk, en hij is de enige bevinding die met de tijd erger wordt in plaats van gelijk blijft.

## Bijlage

Schermafbeeldingen gemaakt tijdens deze doorloop, in
`docs/generale-repetitie-2026-08-13/`:

- `deploymentmodal-tls-per-component.png` - de deploymentmodal met het blok "Certificaat
  (alleen voor deze deployment)": de uitleg dat leeg laten het component volgt, en de
  TLS-keuze die de erfenis benoemt. De dubbele kop uit bevinding 7 staat er ook op.
- `keycloak-configuratiemodal.png` - de keycloak-configuratiemodal, de pagina waarop de
  dubbele `id`'s gemeten zijn. Het aanvinkvakje "Toegang beperken" is het element waarop
  bevinding 2 van de vorige doorloop gemeten werd; dat het aan staat komt doordat de
  wizard-hulpfunctie het bewust aanzet (de auth-wall-stap eist het), niet doordat het
  vakje zijn waarde negeert.
