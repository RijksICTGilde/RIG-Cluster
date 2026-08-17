# De derde generale: alles groen, op een tak die stilstaat

Doorloop van `release-augustus-2026` door een sessie die de wijzigingen niet zelf heeft
gemaakt. RC-118 (de tweede generale) vroeg hier zelf om: zijn oordeel was niet "deze tak kan
naar main", maar "review -> merge-beslissing -> een nieuwe doorloop door een andere sessie,
op een tak die niet meer beweegt". Aan die laatste voorwaarde was toen niet voldaan.

Taaknummer: RC-128. Alle metingen zijn gedaan op de sandbox (Kind-cluster
`kind-rig-sandbox`, `https://zad.sandbox.rijksapp.dev`), niet op productie.

---

## 0. De tak, en of hij stilstond

| | |
|---|---|
| Tak onder toets | `release-augustus-2026` |
| Commit bij het begin | `572be9c8741bf76631f0f661c3223567a7774a24` (`572be9c8`) |
| Commit bij het eind | *(zie het slot van dit verslag)* |

De opdrachtgever heeft de tak bevroren voor de duur van deze doorloop. De eindmeting staat
in het slotoordeel; als de commit verschoven is, is de uitkomst niet het oordeel over de tak
die naar main gaat, en dan staat dat er met zoveel woorden.

## 0.1 De voorwaarden vooraf

Dit is waar de vorige twee rondes geld verloren, dus het staat hier als meting en niet als
aanname.

**Het slot.** `orch sandbox status` zei `FREE`; `sandbox-deploy` heeft het slot geclaimd en
gehouden.

**Het cluster.** `kubectl config current-context` -> `kind-rig-sandbox`.

**De draaiende versie.** Bij het begin draaide het cluster de image van een *andere* PR:

```json
{"version":"85571d92","branch":"de-generatie-loopt-niet-op-en-een-herhaalde-restor",
 "image":"operations-manager:rc-123-"}
```

Dat is exact het gat waar RC-118 vijf gemeten projecten aan verloor. Na `sandbox-deploy`:

```json
{"version":"572be9c8","commit":"572be9c8741bf76631f0f661c3223567a7774a24",
 "branch":"de-derde-generale-alles-groen-op-een-tak-die-stils",
 "pod":"operations-manager-5894fd5bcc-6dgkw","image":"operations-manager:rc-128-"}
```

Vijf keer achter elkaar opgevraagd: vijf keer `572be9c8`, en vijf keer dezelfde pod
(`operations-manager-5894fd5bcc-6dgkw`). Er antwoordt dus geen oude pod meer mee.

**En niet op `/version` vertrouwd.** RC-118 zag dit endpoint twee keer liegen, dus de
sluitende controle is de pod zelf vragen of de code erin zit:

| Controle in de pod | Uitkomst |
|---|---|
| `grep -c "_hertekenNaDeSwap\|_kanDeWaardeDragen" /app/static/js/wizard.js` | `4` — de cascadefix van RC-127 zit er werkelijk in |
| `python -c "import cryptography"` | `48.0.0` — de declaratie uit `65be1dcd` (de laatste commit op de tak) is meegebouwd |
| `ls /app/opi/connectors/transip.py` | aanwezig — de CAA-weg van RC-126 zit er in |

De eerste en de laatste inhoudelijke commit van de tak zijn dus aantoonbaar in de pod. Dit
is per blok herhaald; de uitkomsten staan bij de blokken zelf.

---

## 1. De geautomatiseerde suites

### Wat er gedraaid is

| Suite | Aanroep | Uitkomst | Tijd |
|---|---|---|---|
| Unit | `uv run pytest tests/ -q` (eigen standaardaanroep, geen eigen `-m`) | **9285 passed, 7 skipped, 0 rood** | 7m36s |
| Browser, gang 1 | `uv run pytest -m e2e -q` | **447 passed, 75 skipped, 0 rood** | 11m57s |
| Browser, gang 2 | idem, direct erna | **447 passed, 75 skipped, 0 rood** | 11m48s |
| zad-waker | `go vet ./...` + `go test ./...` | **vet clean, 8/8 PASS** | 5s |
| Sandbox | `uv run pytest -m sandbox -q` tegen het cluster | *(zie "de sandboxsuite")* | |
| Reallife + punt14 | gelijktijdig, zoals RC-112 | *(zie "de sandboxsuite")* | |

De twee browsergangen gaven **byte-identieke uitslagen**: zelfde aantal groen, zelfde
skiplijst. Dat is wat de tweede gang moet aantonen — er zit geen ordeafhankelijkheid en geen
flakiness in.

### De 75 overgeslagen browsertests, uitgesplitst

Een skip is geen groen, dus ze zijn nageteld in plaats van weggewinkt:

| Aantal | Wat | Waarom |
|---|---|---|
| 71 | tests met de `sandbox`-marker | `E2E_BASE_URL` staat in deze aanroep niet, dus ze slaan over volgens ontwerp. Ze draaien apart onder `-m sandbox` |
| 3 | `test_lotc_domeinbeheer::...in_firefox`, `test_lotc_metrics_explorer::...in_firefox` (2x) | **Firefox stond niet op de machine.** Dat is een gat, geen ontwerp |
| 1 | `test_csrf_browser::test_invite_register_has_hidden_csrf_field` | de testapp heeft geen invite-key; skip volgens ontwerp |

De drie Firefox-skips zijn **dichtgezet in plaats van gemeld**: `playwright install firefox`
vroeg vijf ontbrekende systeembibliotheken (`libxcursor1`, `libgtk-3-0t64`,
`libpangocairo-1.0-0`, `libcairo-gobject2`, `libgdk-pixbuf-2.0-0`), en na het installeren
daarvan:

```
uv run pytest -m e2e -q -k "firefox or invite_register_has_hidden_csrf"
==> 3 passed, 1 skipped in 19.89s
```

Dus **0 rood, en van de skips blijft er precies één over die er volgens ontwerp hoort te
zijn.** Wie deze suite in een schone omgeving draait moet Firefox meenemen, anders zijn die
drie visuele tests stil weg.

### De sandboxsuite, en waarom hij twee keer gedraaid moest worden

De eerste gang liep vast. Niet op een test, maar op de node.

**Wat er gebeurde.** Test 31 (`test_sandbox_punt14.py::test_deployment_overleeft_een_gelijktijdige_uitrol`)
stond 35 minuten stil. De oorzaak:

| Meting | Waarde |
|---|---|
| De node | **een** node, `4 cpu`, `16Gi`, **max 110 pods** |
| Het project dat de punt14-tests aanmaken | **25 deployments, 71 pods, 3750m cpu-requests** |
| De node op dat moment | **99% cpu-requests** (3985m van 4000m), **103 van 110 pods** |
| Wat de wachtende pods zeiden | `FailedScheduling: 0/1 nodes are available: 1 Insufficient cpu` |

Een rollende update wil per deployment tijdelijk een pod extra. Bij 25 deployments is dat 25
pods erbij, en die ruimte was er niet, dus werd de rollout nooit `Healthy` en wachtte
`wait_for_application_synced` per app tot zijn timeout van 300s.

**Het is geen codefout, en dat is nagekeken in plaats van aangenomen.** De pollus in
`oom_watcher` doet de normale sweep van 5s over ~60 componenten (dat verklaart de ~20
kubectl-paren per seconde in het log, wat er als een hot loop uitzag), en de wacht in
`argo_manager.wait_for_application_synced` is wel degelijk begrensd. Het is capaciteit.

**Wat ik daarna deed.** De doorloop afgebroken, het testafval van eerdere runs opgeruimd via
de echte weg (`DELETE /api/projects/{naam}` met `confirmDeletion`, zodat er geen ArgoCD-app
of AppProject achterblijft): `p1482-qfi`, `e2e71-jqm`, `e2e71-p3c`, `p1450-8cu`, `rc118-5pf`,
`rc118-tls`. Daarmee ging de node van **99% naar 39% cpu** en van **103 naar 32 pods**.
Daarna de suite opnieuw, zonder de vier punt14-tests.

**En daar kwam een tweede les uit.** In die tweede gang gaven de vijf tests in
`test_sandbox_component_values_api.py` een ERROR: de module-fixture maakt een project via de
echte wizard, en die liep na 246s af. In de eerste gang deed dezelfde test er 22s over en was
hij groen. Losstaand opnieuw gedraaid:

```
uv run pytest tests/e2e/test_sandbox_component_values_api.py -m sandbox -q
==> 5 passed in 191.63s
```

Dus **de code is niet stuk**: ik was de suite gestart binnen twee minuten na het verwijderen
van zes projecten, terwijl namespaces en CNPG-clusters nog aan het opruimen waren. Na een
bulkverwijdering moet het cluster eerst tot rust komen; anders meet de eerste test van de
suite die opruiming. Dat is een meetfout van mij, geen bevinding over de tak.

### De poorten die geen test zijn

| Poort | Uitkomst |
|---|---|
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **1062 files already formatted** |
| `uv run pyright` | **0 errors, 0 warnings, 0 informations** |

### En de poort die niet groen is: de tak merget niet in `main`

Dit staat hier omdat het de merge BLOKKEERT, en dat is volgens het plan het enige waarvoor
een bevinding uit de "wat er buiten valt"-lijst mag komen. Gemeten met `git merge-tree`
tegen `origin/main` in plaats van tegen de gestelde basis:

```
git merge-tree --write-tree origin/main origin/release-augustus-2026   -> exit 1
```

**14 conflicten**, over deze bestanden:

| Soort | Bestanden |
|---|---|
| Inhoudelijk (6) | `opi/core/startup.py`, `opi/forms/visualizers/providers.py`, `opi/manager/project_manager.py`, `opi/services/project_store.py`, `opi/web/router.py`, `workflow/review.md` |
| Inhoudelijk, tests (2) | `tests/test_detail_page_backup_laziness.py`, `tests/test_project_resource_usage.py` |
| modify/delete (6) | `opi/templates/project-details.html.j2` en vijf bestanden onder `opi/templates/project-details/` |

Die laatste zes zijn het zwaarste deel en het is **geen mechanisch conflict**. De releasetak
heeft die sjablonen verwijderd (de LOTC-migratie), en `main` heeft ze in de tussentijd
gewijzigd. Sinds de merge-base (`02ee39fe`) staan er:

- **21 commits op `main`**,
- **1418 commits op de releasetak**.

Van die 21 raken er meerdere precies die verwijderde sjablonen, met werk dat niet in de
LOTC-versie zit:

```
7b721dae fix(project-details): bevestig een delete en herstel geen verwijderde deployment
0993c687 perf(project-details): laad lazy blokken pas als ze in beeld komen
fe490809 feat(project-details): compacte kaart met het totale resourcegebruik van het project
723360ac fix(project-details): één backup-request per project, niet één per deployment
b2abbb08 perf(project-details): haal backup-snapshots lui op, net als de ArgoCD-blokken
```

Dat is geen conflict dat je oplost door een kant te kiezen: dat werk moet **opnieuw op de
LOTC-sjablonen worden gezet**, en de twee conflicterende testbestanden
(`test_detail_page_backup_laziness.py`, `test_project_resource_usage.py`) zijn precies de
tests die daarbij horen. Kiest iemand hier de releasekant, dan valt dat werk stil weg zonder
dat een test het merkt, want de tests die het pinnen vallen in hetzelfde conflict.

Het is **niet gerepareerd** in deze doorloop, en dat is opzettelijk: de tak moest stilstaan
om de rest van dit verslag geldig te houden, en een merge-resolutie over 14 bestanden is een
eigen taak met een eigen review.

### Noot bij zad-waker

Er staat **geen Go** op deze machine, en de docker-daemon deelt `/workspace` niet: een
`docker run -v /workspace/...` mount komt leeg binnen (gecontroleerd: `ls -la` in de
container geeft een lege map). Daarom via `docker build` met `images/zad-waker/` als
build-context, wat de bestanden wél binnenbrengt. Uitkomst:

```
go vet ./...  -> VET_OK
go test -v ./...
  PASS TestWokenFromOutside            PASS TestProbesAreNotVisitors
  PASS TestIdleCadenceIsSlow           PASS TestWaitingExpires
  PASS TestVisitorGetsTheFastCadence   PASS TestWakeInFlightKeepsTheFastCadence
  PASS TestPageVisitCountsAsWaiting    PASS TestDefaultIdleInterval
  ok  github.com/minbzk/base-images/zad-waker  0.867s
```

## 2. De 47 projecten uit de sandboxrepository

### Het aantal, zelf geteld

De opdrachtgever zei 47. Nageteld in `rig-cluster-projects-sandbox`, branch `main`, map
`projects/`: **47 bestanden**, en alle 47 declareren `sandboxed-local` als enige cluster. Er is
dus geen bestand stil overgeslagen.

Wat er in die 47 zit, ook nageteld uit de YAML:

| | Aantal |
|---|---|
| Projecten | **47** |
| Deployments | **137** |
| Componentdefinities | 90 |
| Component-instanties in deployments (~pods bij 1 replica) | **261** |

De grootste vier: `wies` (18 deployments), `regel-k4c` (17), `asses-k2n` en `pm-5sj` (15 elk).
Twee projecten hebben nul componenten (`mb-docs-helmfile`, `mb-grist-helmfile`).

Zes van de 47 stonden al op de sandbox (`algor-odc`, `amt-odc-prd`, `amtbz-2m9`, `cot-zaq`,
`jc-77j`, `mzs-3ik`). Van die zes zijn er twee byte-identiek aan de bron (`cot-zaq`,
`mzs-3ik`); de andere vier zijn op de sandbox sinds de export door OPI zelf bijgewerkt. Bij de
import gaat de **bronversie** erover, want dat is wat de taak vraagt.

Dat de bestanden bij deze sandbox horen is ook echt vastgesteld en niet aangenomen: alle
**47** API-sleutels uit `config.api-key` zijn te ontsleutelen met de platformsleutel van dit
cluster (twee lagen AGE, zoals `ProjectService._resolve_plaintext_api_key` het doet). Nul
mislukkingen.

### Waarom in blokken, en niet in een keer

**261 pods op een node die er 110 aankan.** Zie de sandboxsuite hierboven voor wat er gebeurt
als je dat toch probeert. Daarom per blok: importeren in `zad-projects`, reconcile, de
**asynchrone** refresh afvuren (die geeft direct een task-id terug), wachten tot het cluster
zelf zegt dat het stil is (geen app meer `Progressing`, geen pod meer `Pending` of
`ContainerCreating`), meten, en opruimen zodat het volgende blok ruimte heeft.

Nooit wachten op een synchrone `:refresh` - dat is de les die RC-118 een factor twintig
scheelde.

Blokindeling op een podbudget van 34 (surge en infrastructuur meegerekend):

| Blok | Pods | Projecten |
|---|---|---|
| 6 | 27 | 24 |
| 5 | 34 | 13 |
| 4 | 34 | 4 |
| 3 | 34 | 2 |
| 2 | 34 | 2 |
| 1 | 35 | 1 (`wies`) |
| 0 | 65 | 1 (`regel-k4c`) - past ook alleen niet |

### De metingen per project

*(volgt)*

## 3. Wat er sinds de tweede generale bij is gekomen

Waar het een scherm betreft is er in de browser gemeten, op het levende cluster, en is de
schermafdruk ook echt bekeken. Dat laatste is geen formaliteit: bij punt 9 en punt 8 gaven
mijn eigen selectors eerst "niet gevonden", en op het scherm stond het er gewoon. Een meting
met een verzonnen selector leest als een defect; die eerste uitkomsten staan daarom hieronder
als les en niet als bevinding.

### 1. De cascade in de wizard (RC-127)

`tests/e2e/test_wizard_cascade_tijdens_verzoek.py` forceert het venster (twee keuzes in
hetzelfde script) en eist ook dat het venster echt geraakt is. **Twee keer groen**, in beide
browsergangen.

Het plan vraagt ook naar een andere dienst met een cascade, omdat de bug in elk afhankelijk
keuzeveld zat. Geteld op de bron, per `data-rerender` met zijn `widget=`:

| Aantal | Widget | Waar |
|---|---|---|
| 11 | `SELECT` | cross-domain, TLS-modus (2x), URL-formaat, Basisdomein (2x), Root component, kaal domein, Leveren als (2x), Herhaling, Deployment |
| 2 | `TEXT` | Subdomein, Eigen domein |
| 1 | `CHECKBOX` | keycloak, "Toegang beperken" |
| 1 | `CHECKBOX_GROUP` | de componentstap, "Gebruikte services" |

De fix zelf is **generiek**: een `change`-luisteraar op `document` met filter
`[data-rerender]`, dus geen veldnaam en geen dienst erin. Maar het herstelpad zet
`vers.value = waarde` en zoekt het verse veld op via zijn `name` - en voor een vakje is
`value` niet de stand. Gemeten op de componentstap: het `[data-rerender]`-element is
`<nldd-checkbox-field>`, zijn `name` is de **lege string** (dus `naam ? ... : null` is falsy)
en de inputs zitten in geneste schaduwbomen, nul in de lichte boom.

**Toch klopt het gedrag**, en dat is empirisch vastgesteld en niet weggeredeneerd: de swap
haalt die host niet uit het document, dus `_hertekenNaDeSwap` komt in zijn eerste tak
(`document.contains(bron)`) en tekent gewoon opnieuw. Het waardepad is voor een vakje dode
letter, maar ook niet nodig.

Nieuw in deze PR staat dat vast in `tests/e2e/test_wizard_cascade_aanvinkvakje.py`: een test
op de **vorm** van het veld en een op het **gedrag** in het venster. Verandert de vorm - gaat
de host wel een naam dragen, of wordt hij bij de swap vervangen - dan valt de vormtest, in
plaats van dat het waardepad stil onbruikbaar blijft.

### 2. CAA-records (RC-126)

| Meting | Uitkomst |
|---|---|
| De uit-stand in het opstartlog | `caa_reconciler - INFO - No TransIP credentials configured, skipping CAA reconciliation` |
| `dig CAA rijks.app` | **leeg** |
| `dig CAA rijksapp.nl` | **leeg** |
| `dig CAA rijksapp.dev` | **leeg** |
| Beide `secretKeyRef`s naar `transip-credentials` in de odcn-overlay | `optional: true` |
| `dns_config.py` tegenover de feature-doc | `CAA_TTL = 3600`, `CAA_TAGS = ("issue", "issuewild")`, geen `iodef` - klopt |

De aan/uit-knop werkt precies zoals gedocumenteerd, en de `optional: true` uit `65be1dcd`
staat er op beide plekken - dat was een bootblokkade en die is dicht.

Maar: **de grendel staat in de publieke DNS nog niet aan.** Geen van de drie zones heeft een
CAA-record. Dat is geen fout in de tak (de sandbox heeft geen TransIP-sleutel en die sleutel
is IP-gebonden aan productie), maar wie deze feature als "gedaan" leest, leest hem verkeerd:
er is nog niets uitgerold en de eerste echte uitrol schrijft in publieke DNS.

### 8. Het statusfilter op /admin/approvals

Eerst mis gemeten: ik zocht `select[name="status"]` en vond er nul. Op het scherm bleek het
**geen keuzelijst** maar een knop met een menu - `954fec37` gaf het filter de vorm van de
sorteerknop ernaast. Opnieuw gemeten, met het menu onder die knop als bereik:

| Meting | Uitkomst |
|---|---|
| Statusknoppen op de pagina | **1** (`Status: Alle statussen`) |
| Menu-items eronder | **4**: Alle statussen, Aangevraagd, Goedgekeurd, Afgewezen |
| `?status=requested` | knop `Status: Aangevraagd`, `selected` op **Aangevraagd** |
| `?status=approved` | knop `Status: Goedgekeurd`, `selected` op **Goedgekeurd** |
| `?status=denied` | knop `Status: Afgewezen`, `selected` op **Afgewezen** |
| `?status=onzin` | valt terug op `Status: Alle statussen` |

Dus: het filtert via de URL, de gekozen waarde staat na de swap nog in de lijst (de server
rendert hem mee), en het staat er **niet** dubbel.

**Kanttekening, en die hoort erbij:** op deze sandbox staan **nul** domeinaanvragen. Wat hier
gemeten is, is het mechanisme - selectie, behoud na de swap, eenmaligheid, terugval. Dat het
werkelijk rijen wegfiltert is **niet** gemeten, want er zijn geen rijen. De unittests
(`tests/test_approvals_statusfilter.py`) dekken dat deel wel.

### 9. Het projectenoverzicht

| Meting | Uitkomst |
|---|---|
| De projectcode als chip | **ja** - `amt-odc-prd`, `algor-odc`, `amtbz-2m9`, `cot-zaq`, `jc-77j` staan als aparte chip in de kaart |
| De bewerkdatum ernaast | **ja** - `Bewerkt 17 aug 2026, 11 uur geleden`, in dezelfde regel |
| Sorteren op laatst bewerkt, beide kanten | **ja** - `?sort=bewerkt` en `?sort=bewerkt-op` geven een **exact omgekeerde** lijst |
| Het zoekveld houdt zijn breedte | **ja** - host `416px` voor en `416px` na het typen |
| Het zoekveld houdt de focus | **ja** - focuspad na drie tekens: `NLDD-SEARCH-FIELD > INPUT`, waarde `amt` |
| Het sorteren staat er niet dubbel | **ja** - 1 sorteerknop, en **geen** zichtbare "Meer" in de schaduwboom van de toolbar op 1440px |

Ook hier zat de eerste meting mis: ik gebruikte `?sort=updated&direction=asc`. Die sleutel
bestaat niet, en een onbekende sleutel valt **stil** terug op naam-sortering - dus beide
richtingen gaven dezelfde lijst en dat las als "sorteren werkt niet". De echte sleutels zijn
`bewerkt` en `bewerkt-op`, en er is geen `direction`-parameter.

### 10. De metingen

| Pad | Canvas | Chart.js geladen |
|---|---|---|
| `/projects/jc-77j/deployments/poc` | **0** | **nee** |
| `/projects/jc-77j/metrics/poc` | **6** | ja |

De grafieken staan dus alleen nog op Metrics, en op Deployments wordt de bibliotheek niet
eens ingeladen. Klopt met `cd557e34`.

Twee dingen bij dit punt:

- Het tabblad zit in het **pad**, niet in een `?tab=`-parameter (`tab_from_path` leest het
  derde segment). Met `?tab=metrics` krijg je stil het Overzicht - opnieuw een onbekende
  waarde die terugvalt in plaats van te klagen.
- De kaart die **verhuisd** is, is "Resourcegebruik (heel project)", en die staat nu op
  Overzicht (tussen Configuratie en Deployments) omdat hij over het hele project gaat terwijl
  Metrics per deployment werkt. Op het scherm gezien en bevestigd.
- **Niet gemeten:** "een deployment op een ander cluster zegt waarom er niets te zien is."
  Alle 47 sandboxprojecten en alle bestaande projecten declareren `sandboxed-local`, dus er
  is op dit cluster geen deployment op een ander cluster om die melding mee op te wekken.

## Oordeel

*(volgt)*
