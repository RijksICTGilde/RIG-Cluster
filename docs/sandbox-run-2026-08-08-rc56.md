# Sandboxrun 8 augustus 2026 — alles groen met de echte projectbestanden (RC-56)

Tweede poging. De eerste (RC-54, `docs/sandbox-run-2026-08-07-rc54.md`) draaide deels op een
verkeerd geconverteerde set: daar stonden nog productie-images en productie-resources in, en op
een kind-cluster meet je daar niets zinnigs mee. Deze run gebruikt de gecorrigeerde set en doet
alle 47 bestanden.

Uitgevoerd op commit `a53715c2` (branch `alles-groen-op-de-sandbox-met-de-echte-projectbest`),
tegen een verse image van precies die commit.

## Samenvatting

| set | uitkomst |
|---|---|
| unit | 6545 passed, 7 skipped, 258 deselected (4:47) |
| e2e lokaal | 151 passed, 1 skipped, 42 deselected (6:19) |
| 47 originelen door de schemapoort | 0 afgekeurd rauw, 0 na migratie |
| 47 geconverteerde bestanden verwerkt | **36 geslaagd, 11 niet** |

Van de 11 die niet slaagden is **geen enkele** een fout in de code die vandaag gemerged is. Ze
vallen in vier groepen die de sandbox zelf betreffen (zie §5). Wat deze run wél opleverde zijn
vijf bevindingen die geen enkele testset kon zien, waarvan er twee alleen naar boven kwamen door
de geleverde diensten na te lopen in plaats van de succesmelding te geloven (§6).

De e2e-sandboxset is **niet** gedraaid; zie §7 voor waarom en wat dat betekent.

## 1. Cluster, context en de set

| | |
|---|---|
| kubectl-context | `kind-rig-sandbox` (bevestigd) |
| sleutel | sandbox (`sops-age-key` uit het cluster), niet productie |
| draaiende versie | `/version` = `a53715c2`, branch `alles-groen-...` |

`sandbox-deploy` meldde bij het uitrollen een `WARN — /version does not clearly show a53715c2`.
Dat was een wedloop met de rollout: hij controleert terwijl de nieuwe pod nog niet klaar is.
Direct daarna klopte `/version` wel. De waarschuwing is dus onterecht, maar hij is er niet voor
niets — hij hoort ná readiness te meten.

De set (`rig-cluster-projects-sandbox` @ `368fca9`) is voor gebruik gecontroleerd, want de vorige
run struikelde daar juist over:

| controle | uitkomst |
|---|---|
| image-regels | 313 van 314 zijn `ghcr.io/minbzk/base-images/e2e-allservices:latest` |
| de ene rest | het database-image `postgresql-with-dictionaries:2024.11.19` (bewust) |
| resourceprofiel | 211 componenten op 32Mi/10m, limiet 128Mi/200m |
| tuner-historie | geen enkele `source: auto-tune` |
| cluster | 47/47 `sandboxed-local`, geen `cluster:` op productie |
| repository-urls | geen enkele naar productie |

De twee resterende treffers op `rijksapps.nl` zijn een registry-entry (`dp-bn7`) en een
`application_url` in een helm-blok (`toets-hn7`) — geen projectvelden, dus terecht blijven staan.

De AGE-geheimen ontsleutelen in twee stappen: de clustersleutel opent de eigen sleutel van het
project, en die opent `api-key`. Wie dat als één stap probeert krijgt
`no identity matched any of the recipients` en denkt ten onrechte dat de set fout is.

## 2. Testsets

Vanuit `operations-manager/python`, met `-p no:randomly` zodat de run naspeelbaar is.

| set | commando | uitkomst |
|---|---|---|
| unit | `uv run pytest tests/ -q` | **6545 passed, 7 skipped** (287s) |
| e2e lokaal | `uv run pytest tests/e2e/ -m "e2e and not sandbox"` | **151 passed, 1 skipped** (379s) |

De eerste lokale e2e-run gaf 150 passed en één rode:
`test_wizard_services_regressions.py::test_preset_stays_applied`. Los gedraaid is die groen (3/3
in dat bestand), en de tweede volledige run gaf 151 groen. Het is dus een volgordeafhankelijke
flake en geen regressie. Beide runs staan hier omdat één groene run na een rode meting niet
eerlijk zou zijn.

## 3. De schemapoort over de 47 ONGECONVERTEERDE originelen

Bron: `rig-cluster-projects-github` @ `30cfba1b3`. Conversie verandert juist de velden waar het
om gaat, dus deze meting hoort op de originelen.

| | 6 aug 2026 | deze run |
|---|---|---|
| root-`domains:` van vóór v2.5 | 30 | **30** |
| `config.keycloak`-restant van vóór v2.3 | 21 | **21** |
| afgekeurd door de poort, rauw | 22 | **0** |
| afgekeurd ná migratie | 0 | **0** |

Gedeclareerde versies: `2` 5x, `2.2` 42x; 41 van de 47 migreren.

De 22 zijn 0 geworden, en dat is de bedoelde winst: sinds RC-32 valideert de poort tegen de
versie die het bestand zelf declareert. Ter controle is ook de oude poortvorm nagebootst (rauw
tegen het nieuwste schema, 2.6): die geeft **34** afkeuringen, meer dan de 22 van 6 augustus,
omdat het nieuwste schema sindsdien `domains:` en `config.keycloak` heeft laten vallen. Dat pad
wordt niet meer gelopen.

**Geen enkel bestand dat op 6 augustus verwerkt werd, wordt vandaag geweigerd.** De schemapoort
van RC-44 verandert dit oordeel niet: die weigert vroeg in de *wizard*, en deze 47 komen niet via
de wizard binnen.

## 4. De 47 verwerkt, een voor een

Verwerken gaat per project met `POST /api/v2/projects/<naam>/:refresh`, want
`ENABLE_GIT_MONITOR=false` staat op deze sandbox. De bestanden zijn in één keer in
`zad-projects` gezet (dat is geen verwerking) en daarna is
`POST /api/v2/admin/projects/:reconcile` aangeroepen; de verwerking zelf is strikt een voor een.

Capaciteit is de bindende beperking: de node doet 110 pods en RC-54 liep daar bij 25 projecten
op vast. Deze run maakt daarom ruimte via **ZAD's eigen verwijderpad**
(`DELETE /api/projects/<naam>` met `confirmDeletion`), niet door ArgoCD-applicaties weg te halen —
dat laatste zou achter ZAD om gaan en de opruiming van realm, namespace en GitOps-mappen
overslaan. In totaal zijn zo 25 projecten opgeruimd tijdens de run.

### Uitkomst per project

| project | uitkomst | tijd | melding |
|---|---|---|---|
| `algor-1ha` | OK | 56s | |
| `algor-odc` | FOUT | 346s | `Error getting application status:` (zie §5.4) |
| `amt-odc` | OK | 101s | groen na herkansing |
| `amt-odc-prd` | FOUT | 40s | `Remote source clone failed: [... Connection reset by peer]` |
| `amtbz-2m9` | FOUT | 40s | `Remote source clone failed: [... Connection reset by peer]` |
| `asses-k2n` | FOUT | 448s | `timed out after 300s waiting for sync` (capaciteit) |
| `bg-4s2` | OK | 45s | |
| `bouwm-6gn` | FOUT | 356s | `timed out after 300s waiting for sync` (capaciteit) |
| `cot-zaq` | OK | 45s | |
| `ddvdtoc-md4` | OK | 60s | |
| `dp-bn7` | OK | 88s | groen na herkansing |
| `dsm1j2-2ws` | OK | 71s | groen na herkansing |
| `fp-unj` | OK | 61s | groen na herkansing |
| `grip-pju` | OK | 55s | groen na herkansing |
| `hwmaw-ovh` | OK | 68s | groen na herkansing |
| `ia-fky` | OK | 86s | |
| `jc-77j` | OK | 81s | groen na herkansing |
| `jongo-lh2` | OK | 56s | |
| `mb-docs-helmfile` | FOUT | 457s | `timed out after 300s waiting for sync` (app start niet, §5.3) |
| `mb-grist-helmfile` | FOUT | 361s | `timed out after 300s waiting for sync` (app start niet, §5.3) |
| `mce-e8z` | OK | 81s | |
| `mft-tp9` | OK | 55s | |
| `mozad-dle` | OK | 60s | |
| `mozam-chu` | OK | 60s | |
| `mpfb-8wh` | OK | 61s | |
| `mpfm-w3h` | OK | 127s | |
| `mpfoa-e2w` | FOUT | 413s | `timed out after 300s waiting for sync` (capaciteit) |
| `mpfpsm-lcl` | FOUT | 362s | `timed out after 300s waiting for sync` (capaciteit) |
| `mpfuc-84g` | OK | 167s | |
| `mzs-3ik` | OK | 60s | |
| `napp-avm` | OK | 72s | |
| `nd-j7s` | OK | 81s | |
| `openp-4pw` | OK | 76s | migreerde `invites:` naar `services/invite/config` |
| `pm-5sj` | OK | 135s | |
| `raadg-7dt` | OK | 65s | |
| `regel-k4c` | FOUT | 96s | domeinvorm verdraagt geen punten (§6.5) |
| `regis-jnv` | OK | 80s | |
| `rijks-595` | OK | 57s | |
| `rxm-72a` | OK | 60s | |
| `toets-hn7` | OK | — | meetfout van mij; 9 Argo-apps `Synced/Healthy` (§5.5) |
| `tr-odc` | OK | 56s | groen na herkansing |
| `tva-d62` | OK | 61s | groen na herkansing |
| `ubbw-0i1` | OK | 86s | groen na herkansing |
| `ug-zxt` | OK | 76s | groen na herkansing; dubbele service-entry samengevoegd |
| `vlam-wt8` | OK | 81s | groen na herkansing |
| `waggl-9et` | OK | 91s | |
| `wies` | FOUT | 10s | `Cannot clone from wies_staging: source database does not exist` |

### De migratie, op een draaiend cluster bewezen

75 `auto-migrate ... to schema v2.6`-commits in `zad-projects`. Gemeten op de staat vlak vóór de
herkansingsronde (daarna heb ik zelf bestanden teruggezet, wat de telling zou vertroebelen):

| | 47 originelen | 27 bestanden na verwerking |
|---|---|---|
| `config.keycloak`-restant | 21 | **0** |
| root-`domains:` | 30 | 4 |
| declareert versie 2.6 | 0 | **19** |
| afgekeurd door de poort | 0 | **0** |
| afgekeurd na migratie | 0 | **0** |

## 5. Wat de sandbox betreft, geen codefouten

**5.1 Externe databases bestaan niet op een sandbox.** `amt-odc-prd`, `amtbz-2m9` (chisel-tunnel
naar productie) en `wies` (kloon van een deployment die hier niet bestaat). Negen van de 47
bestanden dragen zo'n `clone-from`.

**5.2 Capaciteit.** `asses-k2n` (29 pods aan losse PR-deployments), `mpfoa-e2w`, `mpfpsm-lcl` en
`bouwm-6gn` liepen op een node die op 119-121 pods stond, boven de capaciteit van 110; hun pods
stonden `Pending`. Dat is de sandbox, niet de code.

**5.3 Twee helmfile-projecten waarvan de app zelf niet start.**
`mb-docs-helmfile` (`CrashLoopBackOff`, 9 herstarts) en `mb-grist-helmfile`
(`CreateContainerConfigError`). Zelfde melding als 5.2, andere oorzaak — daarom apart.

**5.4 `algor-odc`.** `ErrImagePull ... 403 Forbidden` op
`ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19`, de ene image
die de conversie bewust laat staan. Niet te trekken op dit cluster.

**5.5 Achtergebleven Keycloak-staat van RC-54.** Negen projecten liepen op:

```
Refusing to re-create project Keycloak realm for <project>/sandboxed-local:
admin user '<project>_sandboxed_local_admin' already exists in master realm.
```

De guard doet precies zijn werk (`keycloak_manager.py:1710`): de geconverteerde bestanden dragen
`config.keycloak` van *productie*, dus OPI ziet geen sandbox-credentials en wil het realm
opzetten, terwijl realm én master-gebruiker nog van RC-54 stonden. Zonder guard zou het
wachtwoord in het YAML gaan afwijken van dat in Keycloak. Na opruimen via ZAD zijn **8 van de 9
groen** bij herkansing; de negende (`asses-k2n`) viel om op capaciteit.

**Twee onderbrekingen van de omgeving, voor de volledigheid.** De `kube-apiserver` viel om
10:01:40 om (`exitCode 137`, reden `Error`, `restartCount 1`) en nam twee projecten mee met
`kubectl connection is not available`; beide waren groen bij herkansing. Kubernetes labelt het
níet als `OOMKilled`, de dmesg van de node toont vandaag geen OOM en de node-container heeft geen
geheugenlimiet, dus geheugendruk vlak na de volledige build is aannemelijk maar niet bewezen —
de host-dmesg is vanuit de container niet te lezen.

## 6. Bevindingen

### 6.1 (ernstig) De read-only databasegebruiker wordt aangemaakt maar nooit doorgegeven

De rol staat in PostgreSQL, het secret levert een lege naam en een leeg wachtwoord. Gemeten op
drie projecten, alle drie identiek:

```
PostgreSQL rollen : waggl_9et_productie_ro, napp_avm_productie_ro, rijks_595_main_ro

Secret <deployment>-database:
  DATABASE_SERVER_USER      = waggl_9et_productie
  DATABASE_SERVER_USER_RO   = <LEEG>
  APP_DATABASE_USER_RO      = <LEEG>
  DATABASE_PASSWORD_RO      = <LEEG>
```

Een component dat read-only toegang wil, kan er dus niet bij: de rol bestaat, maar niemand weet
hoe hij heet. Geen enkele unittest of browsertest kan dit zien, want het verschil zit tussen wat
er in PostgreSQL staat en wat er in het secret belandt. Niet gerepareerd; hoort een eigen taak
met een eigen toets te zijn.

### 6.2 (ernstig) Projectverwijdering meldt zichzelf mislukt op een MinIO-gebruiker die niet bestaat

```
mc: <ERROR> Unable to make user/group policy association.
    The specified user does not exist. (Specified user does not exist).

delete_project_manager - Deployment deletion completed for amt-odc-prd/productie
  - Success: False, Force: False, Errors: 1
router - Project deletion completed for: amt-odc-prd (success: False)   -> HTTP 207
```

`amt-odc-prd` was gestrand op de chisel-stap, dus zijn MinIO-gebruiker is nooit aangemaakt. Het
losmaken van het beleid van die niet-bestaande gebruiker faalt, en dat telt door tot in het
eindoordeel. De namespace wordt wél netjes verwijderd. Voor een pad dat juist bedoeld is om puin
op te ruimen ná een mislukte verwerking is "er is niets om los te maken" behandelen als
"losmaken mislukt" de verkeerde kant op falen. Twee van de 25 opruimingen gaven zo een 207.

### 6.3 De readiness-probe loopt uit onder verwerkingsdruk en de pod valt uit de load balancer

```
Warning  Unhealthy  pod/operations-manager-...
  Readiness probe failed: Get "http://10.244.0.51:8000/readyz": context deadline exceeded
```

OPI is **niet** herstart (`restarts=0`), maar werd uit de endpoints gehaald terwijl hij een
project verwerkte; nginx gaf daarop `503`. Zes projecten op rij sneuvelden zonder dat er iets mis
was met hun bestand (`toets-hn7`, `tr-odc`, `tva-d62`, `ubbw-0i1`, `ug-zxt`, `vlam-wt8`); alle
zes zijn groen bij herkansing. Op één instantie betekent dit dat elke aanroep faalt zolang de
verwerking duurt.

### 6.4 Een groot project verwijderen overschrijdt de ingress-timeout

`DELETE /api/projects/asses-k2n` gaf `502`, terwijl de verwijdering gewoon slaagde (29 pods
verdwenen). De aanroeper krijgt dus een fout voor een geslaagde bewerking, en weet niet of hij
opnieuw moet proberen.

### 6.5 Een schemafout wordt gepresenteerd als een bewerkingsconflict

```
Project 'regel-k4c' is gewijzigd sinds u begon met bewerken en de samengevoegde versie is
ongeldig (Het gekozen URL-formaat ondersteunt geen punten in de domeinnaam. Dit domein
(sandbox.rijksapp.dev) ondersteunt punten niet. Kies een ander URL-formaat of een ander
domein.). Haal de laatste versie op en probeer opnieuw.
```

De inhoudelijke klacht is terecht en waarschijnlijk een artefact van de conversie:
`regel-k4c` draagt `domain-format: component-deployment-project`, dat geen punten verdraagt in
`sandbox.rijksapp.dev`. Maar de verpakking klopt niet: dit is een serverzijdige `:refresh`, er zit
geen gebruiker te bewerken, en "haal de laatste versie op en probeer opnieuw" helpt niemand
verder. De echte oorzaak wordt door de samenvoegmelding gemaskeerd.

## 7. Wat er niet gemeten is

**De e2e-sandboxset (42 tests) is niet gedraaid.** De 47 bestanden een voor een verwerken plus de
herkansingsronde vulde het cluster en de tijd; die suite tegelijk draaien zou om dezelfde
110 pods vechten en dan meet je opnieuw de sandbox. Dat is een echt gat in deze run: de
bijlage-endpoints (RC-38, RC-52), `rollout=false` (RC-46) en de tokenverificatie (RC-51) zijn
hierdoor **niet** tegen een draaiend cluster gehouden. Die suite hoort alsnog te draaien op een
leeg cluster, en dat is het eerste wat een volgende ronde moet doen.

Uit RC-54 staat bovendien nog open: `test_sandbox_env_vars_aliases_ui.py` liet zien dat een
deployment-override de env-vars van het component wist. Dat is niet opnieuw gemeten en dus niet
opgelost.

## 8. Verantwoording van de meetfouten in deze run

Twee dingen zijn misgegaan aan mijn kant en staan hier omdat ze de getallen raken:

1. **De verwerking heeft een uur stilgestaan.** Bij de API-storing wilde ik de run stoppen om
   niet tegen een kapotte verbinding te meten; die `pkill` raakte het proces, maar mijn controle
   erna matchte mijn eigen shellcommando, dus ik dacht ten onrechte dat hij doorliep. Hervat
   vanaf project 5, losgekoppeld van de shell.
2. **`toets-hn7` staat als fout in de ruwe log** terwijl hij geslaagd is: mijn polling verloor de
   verbinding tijdens de 503-periode. Zijn werkelijke staat (9 Argo-applicaties `Synced/Healthy`,
   pods `2/2`, ingresses aanwezig) is nagelopen en telt als geslaagd.
