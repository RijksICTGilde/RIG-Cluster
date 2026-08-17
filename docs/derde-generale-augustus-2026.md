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
| Sandbox | `uv run pytest -m sandbox -q` tegen het cluster | *(zie hieronder)* | |
| Reallife + punt14 | gelijktijdig, zoals RC-112 | *(zie hieronder)* | |

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

*(volgt)*

## 3. Wat er sinds de tweede generale bij is gekomen

*(volgt)*

## Oordeel

*(volgt)*
