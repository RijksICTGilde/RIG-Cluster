# De tests in vijf lagen, en de snelle lus in een minuut

Kaart: RijksICTGilde/RIG-Cluster#223 (ZAD-bord).

## Aanleiding

Gemeten op 19 oktober 2026, op main `2ac8c130b`, op een machine met 14 cores, alles op één core zoals het nu draait:

| Run | Tests | Duur |
|---|---|---|
| `pytest` (standaard, `addopts` sluit `requires_infra` en `e2e` uit) | 13.484 | 7 min 6 s |
| `task test-e2e` (Playwright tegen de lokale app) | 528 | 11 min 24 s |
| sandbox (`-m sandbox`), `tests/integration/ -m slow`, `requires_infra`, `upgrade_safety` en `keycloak_harness` | 115 + rest | niet gemeten; vraagt een sandbox of infra |

Wie alles draait, zit dus ruim boven het half uur. Waar de tijd zit in de standaardrun (uit `--junitxml`):

| Duur per test | Tests | Samen |
|---|---|---|
| < 10 ms | 11.352 | 10 s |
| 10 tot 100 ms | 847 | 36 s |
| 100 ms tot 1 s | 1.237 | 279 s |
| 1 tot 5 s | 45 | 73 s |
| ≥ 5 s | 3 | 19 s |

84% van de tests is al een echte unittest en kost samen tien seconden. De tijd zit in ongeveer 1.300 tests die een echte binary starten, de app opbouwen of wachten. De traagste bestanden zijn:
- `test_sops_rotation_round` (57 s);
- `test_key_rotation_project_round` (24 s);
- `tests/integration` (20 s);
- `test_kind_registry` (15 s);
- `test_v2_validation` (14 s);
- `test_v2_router` (13 s);
- `test_loop_watchdog` (12 s, 9 tests);
- `test_keycloak_eigen_image` (12 s).

Wat verder opviel:

- **De soort test volgt nu uit markers, en die staan op weinig bestanden.** `tests/` heeft 623 bestanden plat naast elkaar, met `forms/`, `integration/` en `e2e/` als enige mappen. De markers zijn `requires_infra` (30 bestanden), `slow` (27), en `e2e`/`sandbox` in `tests/e2e`. Selecteren gebeurt met een `-m`-reeks in `addopts` en in elke Taskfile-taak, en die reeksen verschillen. Zo sluit `ci.yml` ook `sandbox` en `slow` uit, en `task test` niet.
- **Een echte Postgres hoort al bij de standaardrun.** `tests/conftest.py` start daarvoor een Docker-container. Verder roepen 44 bestanden een echte binary aan (sops, age, kustomize, helm, git, kubectl, gpg), 13 maken een git-repo aan en 7 doen `time.sleep`.
- **Een deel van de unittests hangt van de werkplek af.** Op deze machine falen er 29 op main zonder dat er iets mis is met de code: 24 in `test_keycloak_eigen_image` ("No version is set for command task", exit 126), 4 in `test_secret_rotate` (een lokaal env-secret) en 1 in `test_metrics_scrape_token`.
- **Er draait niets parallel.** `pytest-xdist` staat niet in de dependencies. `pytest-randomly` is er wel, en `task test-e2e-random` toetst daarmee al dat de browsertests niet van hun volgorde afhangen.
- **Een lokale browserrun schrijft getrackte bestanden.** Na `task test-e2e` stonden 56 bestanden onder `tests/e2e/screenshots/lotc/` als gewijzigd in git.
- **Coverage wordt bijgehouden, maar de drempels spreken elkaar tegen.** `ci.yml` draait `coverage run --source=opi` en faalt onder 21%. `pyproject.toml` zegt `fail_under = 90`, dus `task test-coverage` (die ook `coverage report` draait) kan niet groen worden. Er is geen meting per laag en geen trend. RC-310 (ruff-beveiligingsregels) voegt een eigen ondergrens toe voor de modules die beveiliging raken.
- **De dclaude-gates draaien de suites ook.** Hoe de testing-gate ze aanroept is niet gemeten; dat staat open voor de distributedclaude-sessie.

## Doel

Er zijn vijf soorten tests, elk in een eigen map, en je kiest een soort door de map te noemen, zonder een rij opties. De snelle lus (unit en component) draait in ongeveer een minuut. Wat traag of extern is, draait apart en bewust. Coverage is per laag zichtbaar en zakt niet ongemerkt.

| Laag | Map | Wat erin hoort | Mag niet |
|---|---|---|---|
| unit | `tests/unit/` | één functie of klasse doet wat hij zegt | subprocess, socket, bestanden buiten `tmp_path`, `sleep`, de app opbouwen |
| component | `tests/component/` | combinaties binnen OPI: `TestClient`, routers, de store op een tijdelijke repo, templates renderen, mocks voor alles buiten OPI | een echte binary, Docker, netwerk naar buiten |
| infra | `tests/infra/` | echte dingen: Postgres, sops, age, kustomize, helm, git, gpg, Docker, een Kind-registry | een browser, de sandbox |
| ui | `tests/ui/` (nu `tests/e2e/`, zonder `sandbox`) | Playwright tegen de lokale app | de sandbox |
| sandbox | `tests/sandbox/` | de volle integratie op de Kind-sandbox | |

## Plan

Twee fasen. Fase 1 verplaatst geen bestanden en kan direct; fase 2 verplaatst alles in één keer en wacht tot de open hardening-PR's van oktober 2026 gemerged zijn (zie Afhankelijkheden). Namen hieronder zijn voorstellen, tenzij ze als bestaand worden genoemd.

### Fase 1: sneller en eenduidig, zonder verhuizing

1. **Parallel.** `pytest-xdist` in de testdependencies. De Taskfile-taken voor unit, component en ui draaien met `-n auto`.
   - De Postgres-fixture maakt al een database per pytest-sessie. Toets dat elke xdist-worker zo een eigen database krijgt, of geef de naam de worker-id mee (`PYTEST_XDIST_WORKER`).
   - Draai `-p randomly` één keer over de hele standaardrun. Tests die van hun volgorde afhangen, krijgen de bestaande marker `serial` en draaien zonder xdist.
   → toets: de standaardrun met `-n auto` groen, met de duur in de PR; een run met `-p randomly` groen, met de seed in de PR.
2. **Een laag-marker per test, automatisch.** Een hook in `tests/conftest.py` (`pytest_collection_modifyitems`) geeft elke test precies één marker: `unit`, `component`, `infra`, `ui` of `sandbox`.
   - Bron: de bestaande markers (`e2e`, `sandbox`, `requires_infra`, `slow`).
   - Daarnaast de fixtures die een test gebruikt (`test_client`, `async_client` → component; de Postgres-fixture, `age_keypair`, `make_age_keypair` → infra).
   - En voor wat dan nog niet past een lijst in de conftest, per bestand, met de reden. Die lijst komt uit een meting: draai de unit-laag met de guard uit stap 5 in een meldstand, en zet wat hij vangt op de lijst.
   
   `addopts` verliest zijn `-m`-reeks; de standaard wordt `-m "unit or component"`, op één plek.
   → toets: elke verzamelde test heeft precies één laag-marker (een test in de conftest zelf); `pytest --collect-only -m unit`, `-m component` enzovoort tellen samen op tot het totaal.
3. **Eén Taskfile-ingang per laag.** Volgens de afspraak in de repo geen losse scripts, alleen Taskfile-taken:
   - `task test`: unit en component, parallel; de snelle lus;
   - `task test:unit`, `task test:component`, `task test:infra`, `task test:ui`, `task test:sandbox`;
   - `task test:all`: alles behalve sandbox, in die volgorde, en stopt bij de eerste laag die faalt.

   De bestaande taken (`test`, `test-all`, `test-integration`, `test-e2e`, `test-e2e-sandbox`, `test-coverage` en de rest) worden aliassen van deze taken, of gaan weg als niemand ze meer aanroept (zoek in `.github/`, `Taskfile.yaml`, de dclaude-gateconfiguratie en `features/`). `ci.yml` roept dezelfde taken aan, zodat CI en de werkplek niet meer uit elkaar lopen.
   → toets: `task --list` toont de nieuwe taken; `ci.yml` bevat geen eigen `-m`-reeks meer.
4. **Coverage op één plek.** `[tool.coverage]` in `pyproject.toml` is de enige bron.
   - Haal `fail_under = 90` weg of zet hem op de waarde die CI nu hanteert, zodat `task test-coverage` en CI hetzelfde zeggen.
   - Elke laag schrijft een eigen datafile (`COVERAGE_FILE=.coverage.<laag>`, met `parallel = true` voor xdist).
   - `task test:coverage` combineert ze en toont het totaal en het cijfer per laag.
   - CI zet het totaal in de job-samenvatting en bewaart het HTML-rapport als artifact.
   - Geen harde ondergrens behalve het huidige cijfer: CI faalt als het totaal daaronder zakt (ratchet), en de drempel gaat alleen met een bewuste commit omhoog.
   - De ondergrens per module uit RC-310 blijft daarnaast staan.
   → toets: `task test:coverage` lokaal groen; de CI-samenvatting toont het cijfer per laag.
5. **Geen werkplekafhankelijkheid in de snelle lus.** De 29 tests die op een werkplek falen omdat er een tool of een lokaal bestand ontbreekt, gaan naar infra en slaan over met een reden als de tool er niet is. Ze falen dus niet stil en verdwijnen ook niet stil. Een browserrun schrijft geen getrackte bestanden meer: screenshots gaan naar een map onder `.gitignore`, en alleen een expliciete taak (voorstel: `task test:ui:baseline`) werkt de referentiebeelden bij.
   → toets: `task test` groen op een schone checkout zonder `.env.*`-bestanden; `git status` schoon na `task test:ui`.

### Fase 2: de mappen

6. **Verhuizen in één PR.** Een eenmalig script in de PR deelt elk bestand in op de laag-marker uit stap 2. Het verplaatst met `git mv`, zodat de historie meegaat, en past imports tussen testbestanden en paden in fixtures aan. Een bestand met tests in twee lagen wordt gesplitst. Daarna gaat de laag uit de map en niet meer uit de hook: elke map krijgt een `conftest.py` die zijn marker zet. De lijst uit stap 2 vervalt. `testpaths` wordt `tests/unit` en `tests/component`; `pytest tests/infra` kiest één laag.
   → toets: het aantal verzamelde tests per laag is voor en na de verhuizing gelijk; alle lagen groen.
7. **De grenzen bewaken.** `tests/unit/conftest.py` krijgt een autouse-guard die een test laten falen als hij `subprocess` start, een socket opent, `time.sleep` of `asyncio.sleep` met meer dan nul aanroept, of de app opbouwt. `tests/component/conftest.py` weigert subprocess en sockets naar buiten. Een test die een uitzondering nodig heeft, hoort in een andere laag; de guard kent geen uitzonderingslijst.
   → toets: een test in de PR die bewust een subprocess start in `tests/unit/` faalt met een melding die de laag noemt.
8. **Documentatie.** `instructions/testen.md` (voorstel): de vijf lagen, wat erin mag, welke taak welke laag draait, waar een nieuwe test hoort, hoe coverage werkt, en de duur per laag als referentie. `CLAUDE.md` verwijst ernaar, onder "Testing", en de regel "draai niet alles; richt op gewijzigde bestanden" verwijst naar `task test`.

## Wat dit verandert

Wie `task test` draait, krijgt in ongeveer een minuut de unit- en componenttests. Infra, ui en sandbox draaien niet meer vanzelf mee. CI en de dclaude-gates moeten ze dus expliciet aanroepen, met de taken uit stap 3. Geen test verdwijnt: het totaal per laag telt op tot het totaal van vandaag.

## Afhankelijkheden

- Fase 2 wacht tot de ongeveer 80 open hardening-PR's van oktober 2026 (RC-267 tot en met RC-352) gemerged zijn. Vrijwel elk daarvan voegt testbestanden toe in `tests/`, en een verhuizing ervoor zou in elk ervan een conflict geven. Fase 1 raakt alleen `conftest.py`, `pyproject.toml`, `Taskfile.yaml` en `ci.yml`, en kan eerder.
- RC-310 zet een ondergrens per beveiligingsmodule; stap 4 bouwt daarop en vervangt hem niet.
- Hoe de testing-gate van dclaude de suites aanroept: vragen aan de distributedclaude-sessie voor stap 3, zodat de gate dezelfde taken gebruikt.

## Buiten scope

- Tests herschrijven om ze sneller te maken, op wat stap 1 en 5 vragen na. De meting per laag laat zien waar dat daarna loont; `test_sops_rotation_round` en `test_key_rotation_project_round` zijn de eerste kandidaten.
- De Java-tests van de Keycloak-providers (eigen job in `ci.yml`).
- Een harde coverage-eis per module buiten RC-310.

## Afronding

`uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` vanuit `operations-manager/python`. Geen em dashes. Nederlandse prose met de Engelse vaktermen uit de woordenlijst in `CLAUDE.md`.
