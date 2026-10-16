# OPI: de event loop mag niet meer stilstaan

## Waarom

OPI draait FastAPI, de TaskWorker (12 gelijktijdige taken) en alle schedulers op één asyncio event loop (`opi/server.py:118-200`, `TASK_WORKER_CONCURRENCY = 12` in `opi/core/config.py:372`). Elke synchrone blokkade raakt dus alles tegelijk: webverzoeken, alle lopende taken, de schedulers.

Gemeten in productie op 15 oktober 2026, image `2026.10.15.1647-237570c4-dirty`:

- Tussen 18:47:06,9 en 18:47:20,8 logde alleen de refresh-taak van project `nd-j7s`, met één regel per 0,8 s ("Found 1 component reference(s) in deployment 'pr-NN'"), en verder niets. De loop stond 14 s stil.
- In die periode liep een GET van een andere taak (refresh `mb-docs-helmfile`) naar ArgoCD af op zijn timeout van 10 s. ArgoCD antwoordde de calls ervoor in ongeveer 20 ms.
- Die ene verlopen poll brak de hele wacht-lus van 240 s af. De taak faalde met `Error getting application status: TimeoutError`, sloeg de ArgoCD-sync over ("Skipping ArgoCD sync due to critical failures"), en de gebruiker kreeg een fout te zien terwijl de wijziging wel was doorgevoerd.
- Rond 18:38 tot 18:47 faalden meer taken op dezelfde melding (`nd-j7s-pr-63`, een delete-status-check).

De health-probes zien dit niet: die draaien bewust in een eigen thread (`opi/core/probe_server.py`). Er is nergens een signaal van loop-lag.

## Wat bewezen moet worden

1. **Geen enkele stap op het refreshpad houdt de loop langer dan 200 ms vast**, gemeten met een meeloper-taak in de tests, op een project met 25 deployments.
2. **Eén verlopen of mislukte poll laat een ArgoCD-wacht-lus niet meer afbreken.** Alleen een terminale toestand die ArgoCD zelf meldt, of het verstrijken van het budget, beëindigt de lus.
3. **Een stilstand in productie is voortaan zichtbaar en wijst de regel aan**: een metriek voor de loop-lag en, boven een drempel, een log met de stack van de hoofdthread.

Een test die alleen toetst dat `to_thread` wordt aangeroepen, bewijst niets. Toets gedrag: een meeloper-taak die tikt terwijl het trage werk loopt. Het sjabloon daarvoor bestaat al: `tests/test_prometheus_custom_query_blokkeert_niet.py`.

## Basis

- Bouw op forgejo/main. Bij het schrijven van dit plan was dat `e98a2cfaa`. Alle regelnummers hieronder gelden voor die commit; ze verschuiven, zoek op de naam.
- De audit waarop dit plan rust is gedaan op die commit, met metingen op de prod-projectbestanden (60 projecten, 162 deployments; het grootste project heeft 25 deployments). Lokale tijden zijn ongeveer 2 tot 3 keer sneller dan prod onder CPU-throttling (limit 3 cores).

## Het werk

Volgorde is de volgorde van impact. Elk punt heeft een eigen test.

### 1. Eén gedeelde, gecachte jsonpath-compiler

**Probleem.** `jsonpath_ng.ext.parse()` bouwt bij elke aanroep de ply-parser opnieuw op: 7,4 ms per parse, tegen 0,026 ms voor de `find()` erna. `cleanup_removed_services_from_yaml_change` (`opi/manager/delete_project_manager.py:2806-2807`) roept per deployment, voor elk van de 8 cleanable service types, twee keer `deployment_uses_service` aan. Dat zijn 48 parses per deployment in één blok zonder `await`. Lokaal gemeten: wies 6,5 s, pm-5sj 9,6 s, nd-j7s 5,6 s. In prod 15 tot 25 s. Met een cache: 38 ms.

**Fix.** Eén helper in `opi/utils/yaml_util.py` (daar staat `find_value_by_jsonpath` al), bijvoorbeeld:

```python
@functools.lru_cache(maxsize=4096)
def compiled_jsonpath(path: str) -> JSONPath: ...
```

Laat alle directe `jsonpath_parse(...)`-aanroepen daarnaar wijzen: `opi/handlers/project_file_handler.py:878`, `opi/utils/yaml_util.py:176` en `:293`, `opi/manager/keycloak_manager.py:801`, `opi/manager/project_manager.py:7381` en `:9538`. Zoek daarna met `grep -rn "jsonpath_parse\|jsonpath_ng" opi` of er geen enkele ongecachte aanroep overblijft. De paden bevatten deploymentnamen, dus 4096 is ruim.

**Veiligheid: geen kruisbestuiving tussen projecten of taken.** Dit is een harde eis van de gebruiker. De cache mag alleen de gecompileerde expressie bewaren, nooit data of resultaten:

- De cachesleutel is uitsluitend de padstring. Geen data, geen project, geen private key in de sleutel of in de gecachte waarde.
- Gecontroleerd in `jsonpath_ng` 1.8.0: op de expressie-nodes (`Child`, `Fields`, `Filter`, `Expression`, `Iterable` enz.) worden attributen alleen in `__init__` gezet. `find()` en `update()` maken nieuwe `DatumInContext`-objecten of muteren de meegegeven data, niet de expressie. Delen tussen gelijktijdige taken is daardoor veilig. Herhaal die controle in de PR voor de versie in `uv.lock`.
- Gebruik de cache alleen voor `find()`/`update()` op een expressie. Geef nooit de gecachte expressie zelf door aan code die er attributen op zet.
- Alles wat de extractors teruggeven, blijft gelijk aan nu: een decrypt met `private_key` gebeurt na de `find()`, per aanroep, en komt niet in de cache.

**Toets voor de veiligheid**, naast de prestatietoets: hetzelfde pad (met een filter, bijvoorbeeld `$.deployments[?@.name=='prod'].components[*].reference`) eerst op project A en daarna op project B. B krijgt alleen zijn eigen waarden. Daarna weer A: weer alleen A. Ook met twee taken die dit gelijktijdig doen via `asyncio.gather` en `to_thread`. En: `compiled_jsonpath(p) is compiled_jsonpath(p)`, en na een `find()` is de expressie gelijk (`==`) aan een verse `jsonpath_parse(p)`.

Hiermee verdwijnen ook: `_build_expected_resources` in `opi/jobs/reconciliation.py:79/82` (5,6 s lokaal over de hele vloot, draait nachtelijk en via de admin-endpoints `admin_router.py:97/199/228`), de jsonpath-aanroepen in de managers (`database_manager.py:1640`, `minio_manager.py:818`, `redis_manager.py:330`, `mail_manager.py:700`), en de backup/restore-modal (`web/router_detail_edit.py:1756`, ongeveer 1,7 s bij 25 deployments).

**Test.** In `tests/test_service_change_detection.py`: een fixture-project met 25 deployments (geanonimiseerd, onder `tests/fixtures/projects/`). `cleanup_removed_services_from_yaml_change` draait terwijl een meeloper tikt; de grootste gat tussen twee tikken blijft onder 200 ms. Daarnaast in `tests/test_marked_for_deletion.py` dezelfde toets voor `_build_expected_resources`. Draai de toets eerst zonder de fix en noteer in de PR dat hij dan faalt.

### 2. ArgoCD-wacht-lussen breken niet af op een transportfout

**Probleem.** `ArgoConnector.get_application_status` (`opi/connectors/argo.py:395-445`) maakt van elke uitzondering, ook een `TimeoutError` van aiohttp, een `RuntimeError`. `ArgoManager.wait_for_application_synced` (`opi/manager/argo_manager.py`, rond `:1392`, `except RuntimeError, TimeoutError: raise`) en `wait_for_infrastructure_ready` (rond `:1206`) gooien elke `RuntimeError` door. De `except Exception` erachter, die juist bedoeld is om door te pollen, wordt zo nooit bereikt voor een timeout.

**Fix.** Onderscheid twee soorten fouten. Een transportfout (timeout, connect-fout, 5xx) betekent opnieuw pollen binnen het budget, met een waarschuwing in de log. Een terminale toestand die ArgoCD meldt (sync failed, degraded na de grace, app verdwenen) beëindigt de lus zoals nu. Gebruik daarvoor een eigen uitzonderingstype voor de transportfout in plaats van `RuntimeError`, en laat de bestaande aanroepers van `get_application_status` (zie `grep -rn get_application_status opi`) zich niet anders gedragen dan nu, behalve in de wacht-lussen.

Tel de verstreken tijd in beide lussen met de wandklok (`asyncio.get_running_loop().time()`) in plaats van `elapsed_time += poll_interval` (`argo_manager.py:1204` en `:1389`). Anders loopt de echte wachttijd uit bij een trage loop en liegt de log over de duur.

**Test.** Een gestubde connector die de eerste poll een `TimeoutError` geeft en daarna Synced/Healthy: de lus slaagt. Een stub die alleen timeouts geeft: de lus stopt pas na het budget, met een `TimeoutError` die het budget noemt. Een stub die een terminale fout meldt: de lus stopt direct, zoals nu.

### 3. SOPS niet meer synchroon op de loop

**Probleem.** `encrypt_to_sops_files` (`opi/utils/sops.py:165`) loopt over alle `*.to-sops.yaml` van een deployment en start per bestand synchroon `sops --decrypt` (`:152`, voor de skip-if-unchanged) en zo nodig `sops --encrypt` (`:229`). Dat gebeurt bij elke refresh, per deployment, in één blok. Aanroepers van `encrypt_to_sops_files_or_fail`: `project_manager.py:2727, 4255, 4375, 4884, 5304`, `argo_manager.py:341, 912`. Daarnaast decrypt `_existing_secret_pairs` (`project_manager.py:340-344`) via `_write_secret_file` (`:1655`).

Er is geen SOPS-library in gebruik: beide paden starten het `sops`-binary, `opi/utils/sops.py` synchroon en `opi/handlers/sops.py:447` async. Stap niet over op een library; dat valt buiten deze taak.

**Fix.** Op elke aanroeper `await asyncio.to_thread(encrypt_to_sops_files_or_fail, ...)`. Voor `_existing_secret_pairs` idem, of de decrypt vooraf doen zodat `_write_secret_file` niet async hoeft te worden; kies wat de kleinste wijziging geeft. `to_thread` is hier volwaardig: de thread wacht op een subprocess en houdt de GIL niet vast. Er bestaat al een async sops-pad in `opi/handlers/sops.py:447`; kijk of dat te hergebruiken is voordat je iets nieuws schrijft.

**Test.** In de stijl van het sjabloon: een `sops`-stub die 300 ms slaapt, drie bestanden; de meeloper blijft tikken (geen gat boven 100 ms).

### 4. Loop-lag zichtbaar maken

**Fix.** Een heartbeat-taak op de loop zet elke 100 ms een tijdstempel. Een daemon-thread (naast de probe-server in `opi/core/probe_server.py`, of in een eigen module die bij startup wordt gestart) meet de achterstand:

- Een metriek `opi_event_loop_lag_seconds` via de bestaande collector in `opi/core/metrics.py` (`GaugeMetricFamily`, geen nieuwe dependency). Exporteer minimaal de grootste lag sinds de vorige scrape, zodat pieken tussen scrapes niet verdwijnen.
- Bij een achterstand boven 1 s: één WARNING per stilstand met de stack van de hoofdthread (`sys._current_frames()[main_thread_id]`, `traceback.format_stack`). Niet elke 100 ms opnieuw loggen zolang dezelfde stilstand duurt.
- De drempel als instelling in `opi/core/config.py`, standaard 1 s.

De log-watcher (`opi/services/log_watcher.py`) pikt WARNING niet op als fout; dat is goed zo, voeg hem niet aan zijn patronen toe.

**Test.** Start heartbeat en watchdog, blokkeer de loop 1,5 s met `time.sleep`: er komt precies één WARNING met de naam van de testfunctie in de stack, en de metriek toont minstens 1,4 s. Blokkeer 0,3 s: geen WARNING.

### 5. ArgoCD-connector: geen synchrone login, één sessie

**Probleem.** `ArgoConnector.__init__` (`argo.py:106-115`) logt synchroon in met `requests.post` (`:155`, timeout 10 s, ongeveer 700 ms) zolang de procesbrede token-cache leeg is. Zolang ArgoCD onbereikbaar is, doet elke nieuwe connector (22 aanroepers van `create_argo_connector`) dat opnieuw, tot 10 s per keer. Verder maakt `_make_authenticated_request` per request een nieuwe `ssl.create_default_context()` (`:251`, laadt de CA-bundel van schijf, ook bij plain http) en een nieuwe `ClientSession`.

**Fix.** Haal de synchrone login uit `__init__`; `_ensure_authenticated` (`:257`) logt al async in, met lock. Haal daarna `requests` uit `argo.py` als niets het meer gebruikt. Maak de ssl-context één keer per proces aan. Een gedeelde `ClientSession` is beter maar vraagt levenscyclusbeheer bij shutdown; doe dat alleen als het klein blijft, anders alleen de ssl-context.

**Test.** Een connector construeren doet geen netwerkverkeer (stub `requests.post` die faalt als hij wordt aangeroepen). De eerste async call logt in.

### 6. Jinja2: templates één keer compileren

**Probleem.** `render_template` (`opi/generation/manifests.py:126-165`) en `ManifestGenerator.template_manifest` (`:181-219`) maken per manifest een nieuwe `Environment` en compileren met `from_string`. Er is geen template-cache; `deployment.yaml.jinja` compileren kost alleen al 12 ms, ongeveer 30 ms per component.

**Fix.** Eén `Environment` op moduleniveau (met de `yaml_scalar`-filter) en `env.get_template(...)` op een pad relatief aan `MANIFESTS_PATH`, of een `functools.lru_cache` op pad naar `Template`. Houd het gedrag van beide huidige Environments gelijk (`trim_blocks`, `lstrip_blocks`, `keep_trailing_newline`, `autoescape=False`); als ze verschillen, houd dan twee instanties.

**Test.** De bestaande manifesttests blijven groen en de uitvoer is byte-gelijk; voeg een toets toe dat twee renders van hetzelfde template één compile kosten.

### 7. De overige synchrone subprocessen en bestandsbomen

Elk klein, elk met `asyncio.to_thread` of een bestaand async equivalent:

- **Chisel-tunnel** (`opi/connectors/chisel_connector.py:116`, `time.sleep(0.5)`-lus met synchrone socket-connect op `:247-272`, tot 30 s). Aanroeper `opi/utils/chisel_helper.py:66`. `await asyncio.to_thread(connector.start_tunnel, ...)` en idem `stop_tunnel`.
- **age-encryptie** (`opi/utils/age.py:108-140`, `encrypt_age_content_sync` en `encrypt_file_to_age_block_sync` starten het `age`-binary). Decryptie gaat al in-process via `pyrage` (`age.py:23`, RC-218). Doe encryptie ook met `pyrage`, met ASCII-armor. Toets dat de uitvoer leesbaar blijft voor de bestaande decrypt én voor het `age`-binary en voor `sops` waar dat van toepassing is; de bytes hoeven niet gelijk te zijn (age is niet deterministisch).
- **Plaintext-guard in de git-connector** (`opi/connectors/git.py:1182-1213`). `os.walk` daalt af in `.git` omdat `dirs` niet gesnoeid wordt. Voeg `dirs[:] = [d for d in dirs if d != ".git"]` toe. Test: een `.git` met een bestand dat de guard zou laten afgaan, wordt niet meer bekeken.
- **copytree/rmtree** op het refreshpad: `project_manager.py:4638-4639`, `:4663-4664`, `:5023-5025` (helm en helmfile). `to_thread`. De delete-paden (`delete_project_manager.py:522, 767, 934, 1008, 1973, 2666`, `reconciliation.py:917`) idem.
- **Singleton-probes** met `subprocess.run(..., timeout=10)` in `__init__`: `kubectl.py:148`, `kopia.py:219`, `skopeo.py:132`, `minio_mc.py:74`. Bij startup construeren (in de lifespan van `opi/server.py`) zodat de eerste constructie niet midden in een taak valt. Gebruik voor kubectl de async `_test_connection` die al bestaat (`kubectl.py:174`) als dat eenvoudiger is.
- **Alleen sandbox:** `opi/connectors/prometheus.py:753-760` en `:1012-1020` wachten binnen `async def` met `future.result()`; vervang door `asyncio.gather(*(asyncio.to_thread(...)))`. `opi/core/cluster_config.py:1216` start `openssl` per component; zet `functools.cache` op de hash.

## Buiten deze taak

Bewust niet in deze taak, omdat ze groot zijn of eerst gemeten moeten worden met de watchdog uit punt 4. Ze komen elk in een eigen plan.

- **Keycloak-connector naar async.** `opi/connectors/keycloak.py` doet ongeveer 300 synchrone aanroepen via python-keycloak 7.1.1 binnen `async def`, met de standaard timeout van 60 s. De async `a_*`-methodes bestaan in die versie maar worden niet gebruikt. Dat is een mechanische maar grote wijziging die een eigen review verdient.
- **Minder kopieën en parses van projectdata.** `ProjectStore.read_path` (`project_store.py:943`) en `get_contents` (`project_manager.py:7303-7343`) maken deepcopies van ruamel-CommentedMaps (15 ms per keer bij ongeveer 40 aanroepers), en een save doet dump plus herlaad (ongeveer 235 ms voor het grootste project).
- **ArgoCD `list_applications` server-side filteren** (`argo.py:526`, aangeroepen op elke projectpagina en in de poll van 2 s in `simple_background.py:74`). De payloadgrootte is niet gemeten.

## Randvoorwaarden

- Niets uitrollen. Geen wijziging aan clusters, geen push naar GitHub.
- Geen gedragswijziging buiten wat hierboven staat. Waar een fix het gedrag zou veranderen (bijvoorbeeld welke fouten een wacht-lus beëindigen), staat dat expliciet in de PR.
- Zoek eerst of een helper bestaat voordat je hem schrijft (`opi/utils/`, `opi/services/project.py` heeft een padlaag zonder jsonpath). Schrijf je toch iets nieuws naast iets bestaands, zeg dan in de PR waarom.
- Code-stijl volgens `CLAUDE.md`: imports bovenaan, moderne type hints, specifieke exceptions, minimaal commentaar, Nederlandse prose met Engelse vaktermen, nooit em dashes.
- Dode code die je tegenkomt (`decrypt_sops_file` en `encrypt_sops_file` in `opi/utils/sops.py:72, 98` hebben geen aanroepers) noemen in de PR, niet verwijderen.

## Klaar als

- Alle toetsen hierboven staan er en zijn groen; voor punt 1 en 3 staat in de PR dat ze zonder de fix faalden.
- `grep -rn "jsonpath_parse(" opi` toont alleen de gecachte helper.
- `grep -rnE "subprocess\.(run|Popen|check_output)" opi` toont alleen plekken die via `to_thread` of bij startup lopen, met een regel in de PR per overgebleven plek.
- `cd operations-manager/python && uv run ruff check . && uv run ruff format --check . && uv run pyright` is schoon.
- De bestaande testsuite is groen.
- Een feature-document `features/event-loop-lag-watchdog.md`: wat de metriek en de WARNING betekenen, de drempel, en hoe je een stack uit de log leest.
