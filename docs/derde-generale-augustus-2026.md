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
| 3 | `test_lotc_aanvragenbeheer::...in_firefox`, `test_lotc_metrics_explorer::...in_firefox` (2x) | **Firefox stond niet op de machine.** Dat is een gat, geen ontwerp |
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

**De aantallen, met de noemer erbij.** Zelf nageteld met `--collect-only`:

| Selectie | Aantal |
|---|---|
| `-m sandbox` | **74** |
| waarvan `-m "sandbox and punt14"` | 4 - niet draaibaar op deze node, zie hierboven |
| `-m "sandbox and not punt14"` - de noemer van de tweede gang | **70** |

De tweede gang zelf gaf **63 passed, 1 failed, 5 errors** (1:14:48). Dat zijn 69 uitkomsten
op 70 tests; de 70e is een voorwaardelijke skip (verscheidene sandboxtests slaan hun
clusterdeel over als `kubectl` iets niet teruggeeft), maar welke dat was heb ik niet
vastgelegd en reken ik dus niet als groen mee. Na de reparatie van de ene failure en na de
vijf errors losstaand te hebben herhaald: **69 van de 70 groen, 1 overgeslagen, 0 rood** -
plus 4 punt14-tests die niet gedraaid zijn. Een eerdere versie van dit verslag zei "de 66
tests"; dat getal komt met geen enkele telling overeen.

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

**Niet gedaan.** Dit is het gat in deze doorloop en het staat hier als zodanig, niet
weggeschreven.

Twee oorzaken, in deze orde van gewicht:

1. **De omgeving.** 261 pods passen niet op een node van 110, dus dit moest in zeven blokken
   met opruimen ertussen. Het gereedschap daarvoor is af en getoetst
   (`blokken.py`: importeren -> reconcile -> asynchrone refresh -> wachten tot het cluster stil
   is -> meten op ArgoCD-health, podstatus, URL's en de projectenrepo -> opruimen), en de
   blokindeling hierboven komt daaruit. Er is geen enkel blok mee gedraaid.
2. **Mijn eigen tijdsbesteding.** Ik heb de eerste sandboxgang 35 minuten laten lopen voordat
   ik doorkreeg dat hij op de node vastliep in plaats van op een test, en ik heb in de eerste
   uren op achtergrondruns gewacht met poll-lussen in plaats van er ander werk naast te doen.
   Die twee dingen samen zijn ongeveer het budget dat taak 2 nodig had. De les staat nu in
   `workflow/build.md` zodat de volgende doorloop hem niet opnieuw betaalt.

Wat er wel uit de voorbereiding vaststaat en bruikbaar is voor wie dit oppakt:

- het aantal (47) en de inhoud (137 deployments, 261 pod-instanties) is nageteld;
- alle 47 API-sleutels zijn ontsleutelbaar met de platformsleutel van dit cluster, dus de
  bestanden horen bij deze sandbox en de import kan zonder verrassingen;
- `regel-k4c` (65 pods) en `wies` (35 pods) passen op deze node niet in een blok met iets
  anders, en `regel-k4c` past ook alleen niet;
- `algor-odc` is nu al een voorbeeld van de categorie "niet onze fout": zijn initdb-pod staat
  17 uur in `ImagePullBackOff` op `ghcr.io/rijksictgilde/...`, een image die dit cluster niet
  kan trekken.

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
keuzeveld zat. Geteld op de bron met een AST-telling over `opi/` - elke
`EditableVisualizer` met `data-rerender` in zijn `attributes`, per `widget=`. In totaal
**23 velden**:

| Aantal | Widget | Waar |
|---|---|---|
| 19 | `SELECT` | cross-domain (8: Bron-project, Bron-deployment, Mijn component, Doel-project, Doel-deployment, Doel-component, Regel 2x), publish-on-web (6: TLS-modus, TLS-modus (deze deployment), URL-formaat, Basisdomein, Root component, kaal domein), deployments (4: Basisdomein, Herhaling, Deployment, Leveren als), bijlagen (1: Leveren als) |
| 2 | `TEXT` | Subdomein, Eigen domein (beide publish-on-web) |
| 1 | `CHECKBOX` | keycloak, "Toegang beperken" |
| 1 | `CHECKBOX_GROUP` | de componentstap, "Gebruikte services" |

Een eerdere versie van dit verslag zei **15 velden waarvan 11 SELECT**. Dat getal komt uit
RC-127 en is te laag: het is een grep-telling, en die telt de acht cross-domain-velden als
**een**, omdat ze een gedeelde constante `_CASCADE` als `attributes` meekrijgen in plaats van
een eigen dict. Basisdomein en "Leveren als" staan bovendien elk op twee plekken. Alleen het
getal en de noemer veranderen; de niet-SELECT-getallen (2 / 1 / 1) klopten wel, en dat zijn
juist de velden waar de conclusie hieronder over gaat.

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

### 3. De wekker (RC-124)

Gedeeltelijk. De twee snelheden staan vast in de Go-tests van `images/zad-waker`, en die zijn
alle acht groen: `TestWokenFromOutside`, `TestIdleCadenceIsSlow`, `TestVisitorGetsTheFastCadence`,
`TestPageVisitCountsAsWaiting`, `TestProbesAreNotVisitors`, `TestWaitingExpires`,
`TestWakeInFlightKeepsTheFastCadence`, `TestDefaultIdleInterval`. Op de bron:
`idlePollInterval = 30 * time.Second` tegen een snelle cadans van 3s, dus **120 statusvragen
per uur zonder wachtende bezoeker** in plaats van 1200. En wie wel wacht krijgt nog steeds de
3s-cadans; dat is wat `TestVisitorGetsTheFastCadence` en `TestWakeInFlightKeepsTheFastCadence`
pinnen.

Op het cluster is `test_sandbox_sleep_mode.py::test_sleep_then_wake_via_waker_page` groen
(343.8s), dus slapen en wakker worden via de wekkerpagina werkt echt.

**Niet gemeten:** de statusvragen per uur op het draaiende cluster geteld. Dat vraagt een uur
kijken naar een slapende deployment, en dat is niet gebeurd.

### 4. De restore-generatie (RC-123)

| Test | Uitkomst |
|---|---|
| `test_sandbox_restore_generation.py::test_twee_restores_geven_twee_databases_en_verdubbelen_de_rijen_niet` | **PASSED** (93.0s) |
| `test_sandbox_restore_extra_schema.py::test_twee_generatie_restores_met_een_extra_schema` | **PASSED** (119.7s) |
| `test_sandbox_restore_op_slot.py::test_wijziging_na_een_restore_slaagt` | **PASSED** (69.5s) |
| `test_sandbox_restore_van_buiten.py::test_restore_van_buiten` | **PASSED** (59.1s) |

Twee restores achter elkaar geven dus een andere doelnaam en de rijen verdubbelen niet - dat is
precies de claim, en de test telt de rijen zelf.

### 5. De bijlagen (RC-119)

| Test | Uitkomst |
|---|---|
| `test_sandbox_secret_rollout.py::test_een_vervangen_bijlage_bereikt_de_draaiende_pod` | **PASSED** (46.9s) |
| `test_sandbox_secret_rollout.py::test_een_gewijzigde_env_var_bereikt_de_draaiende_pod` | **PASSED** (56.3s) |

En die 202-met-task-id is hier ook echt gemeten, van de andere kant: de TLS-doorloop viel om
omdat hij nog 200/201 verwachtte op de bijlage-upload. Zie de reparatie hieronder.

### 6. De slaapstand (RC-119)

Alle vijf groen: `test_sandbox_sleep_mode.py` (1) en `test_sandbox_sleep_mode_ui.py` (4,
waaronder `test_deployment_sleep_wake_toggle` en `test_wizard_wrote_sleep_config`).

### 7. De takenlijst

Gedeeltelijk. Op `/tasks` is gemeten dat **geen enkele afgeronde taak aanklikbaar is** - dat is
de helft die `b66d717c` toevoegde. De andere helft (een LOPENDE taak is aanklikbaar en leidt
naar de voortgang) is **niet** gemeten: op het moment van kijken stonden er nul taken in de
lijst, en afgeronde taken worden opgeruimd. De unittests
(`tests/test_taken_voortgang_link.py`) dekken beide kanten wel.

### 11. Dienst binden aan een component

Gedeeltelijk, en de reden staat erbij. `POST /api/v2/projects/jc-77j/services` met
`{"service": "keycloak", "components": ["frontend"]}` terwijl keycloak al op projectniveau
staat:

```
-> 202 accepted, task_type=add_service
taak -> completed
result: services_added=[], services_skipped=["publish-on-web","keycloak"],
        processing={"status":"skipped"}
```

Dus het **slaat op** en de dienst die al op projectniveau stond wordt netjes als `skipped`
gerapporteerd in plaats van als fout - dat is het gedrag dat de API-beschrijving belooft
("configure-then-bind werkt in beide richtingen"). Maar **"EN rolt uit" is niet aangetoond**:
dit component had die binding al, dus er was niets te doen en `processing` is terecht
`skipped`. Om die helft te meten is een dienst nodig die op projectniveau staat en nog niet op
het component; dat is niet gedaan.

### 12. Het opslaan van een project met een versleuteld veld (de RC-118-blokkade)

Dit gaf in RC-118 permanent "Project is gewijzigd sinds je begon met bewerken". Vijf keer
achter elkaar een AGE-versleuteld aliasveld opgeslagen op `jc-77j`:

```
opslag 1/5 -> 202  conflict=False   taak -> completed
opslag 2/5 -> 202  conflict=False   taak -> completed
opslag 3/5 -> 202  conflict=False   taak -> completed
opslag 4/5 -> 202  conflict=False   taak -> completed
opslag 5/5 -> 202  conflict=False   taak -> completed
```

**Geen enkele conflictmelding.** De blokkade is weg. De fix zit in `4f483796` (de driewegmerge
struikelde over containervormen, niet over inhoud) na een eerdere poging die is teruggedraaid
(`8c6d1013` -> `9b2ab3b5`); alle drie staan op deze tak.

Onderweg een meetfout die het vermelden waard is: mijn eerste poging schreef aliassen met een
vrije waarde en kreeg vijf keer een **422** - "de alias verwijst niet naar een
platformvariabele". Dat is een validatie en geen conflict, maar het raakt de opslagweg dus
helemaal niet. Met `$OIDC_URL` als waarde raakt hij hem wel. Een 422 die je voor een geslaagde
meting aanziet is een groen dat niets bewijst.

---

## De reparatie die deze doorloop wel gedaan heeft

`tests/e2e/test_sandbox_tls_override.py::test_doorloop_van_de_tls_override` was rood. Niet op
een productfout:

```
POST /api/v2/projects/{p}/services/attachments/attachment
-> 202 {"status":"accepted","task_id":"...","task_type":"configure_attachment", ...}

assert upload.status_code in (200, 201)   # de test
```

Die weg is **asynchroon geworden** - dat is punt 5 van dit plan, de bijlage die zichzelf
uitrolt. De test eiste nog het synchrone antwoord en las de catalogus voordat de taak had
gelopen. Aangepast: 202 toegestaan en dan op de taak wachten, met 200/201 nog steeds geldig
zodat hij niet omslaat als deze weg ooit weer synchroon wordt.

```
voor:  1 failed, 2 passed
na:    3 passed          (278.34s)
```

Dat was de laatste rode in de sandboxsuite die geen omgevingsartefact was.

---

## Oordeel

**Deze tak kan niet naar main, en de reden is geen test: hij merget niet.**
`git merge-tree --write-tree origin/main origin/release-augustus-2026` geeft **14 conflicten**,
waarvan zes modify/delete op `opi/templates/project-details*` - sjablonen die `main` in 21
commits heeft doorontwikkeld (delete bevestigen, lui laden, de resourcekaart, een backup-request
per project) en die deze tak in de LOTC-migratie heeft verwijderd. Dat werk moet opnieuw op de
LOTC-sjablonen worden gezet, en de twee tests die het pinnen vallen in datzelfde conflict, dus
geen enkele test merkt het als iemand hier de releasekant kiest.

**De tak is tijdens de hele doorloop blijven stilstaan op `572be9c8741bf76631f0f661c3223567a7774a24`**
- gemeten bij het begin, tussendoor en aan het eind, met `git fetch` ertussen. De uitspraken in
dit verslag gaan dus over de tak die naar main zou gaan, en dat was in RC-118 juist niet zo.

Wat er verder over te zeggen valt, in het kort:

- **Alles wat losstaand te draaien is, is groen.** Unit 9285 passed / 0 rood. Browser 2x 447
  passed / 0 rood met identieke uitslagen. zad-waker 8/8. ruff, ruff format en pyright schoon.
  De sandboxsuite is na de reparatie hierboven en na herhaling van de vijf omgevingsfouten
  **0 rood op de 70 tests die op deze node kunnen draaien** (74 met de `sandbox`-marker, min
  de 4 punt14-tests): 69 groen en 1 voorwaardelijke skip die ik niet heb vastgelegd.
- **Vier tests zijn niet gedraaid** (`-m punt14`): ze bouwen zelf een project van 25
  deployments en 71 pods en vullen daarmee de node, dus hun rollout kan niet slagen. Dat is een
  grens van de sandbox, geen uitspraak over de tak.
- **Taak 2 is niet gedaan.** Zie daar; de oorzaak is deels de omgeving en deels mijn eigen
  tijdsbesteding, en beide staan er met naam bij.
- **Van de twaalf punten van taak 3 werken er acht aantoonbaar** (1, 2, 4, 5, 6, 9, 10, 12),
  en vier gedeeltelijk met de ontbrekende helft expliciet benoemd (3, 7, 8, 11). Geen enkel punt
  is stuk gebleken.
- **Eén ding dat "gedaan" lijkt maar het niet is:** de CAA-grendel staat in de publieke DNS nog
  niet aan. Geen van de drie zones heeft een CAA-record. De code werkt, de uit-stand werkt, de
  bootblokkade is dicht - maar de eerste echte uitrol schrijft in publieke DNS en is nog niet
  gebeurd.
