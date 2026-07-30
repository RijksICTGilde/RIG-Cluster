# OOM auto-tune: een deployment mag zichzelf altijd ophogen

**Status**: Voorstel, nog niet geïmplementeerd
**Aangemaakt**: 2026-07-30
**Aanleiding**: `asses-k2n/pr-450` op odcn-production

## Context

Op 30 juli 2026 08:16 faalde de deploy van `asses-k2n/pr-450` met:

```
asses-k2n-pr-450: OOM detected for api but auto-tune could not determine new limits
```

De OOM-detectie werkte prima. `oom_watcher` zag de OOMKill op `pr-450-api` (exitCode 137)
en riep de auto-tune aan. Die deed vervolgens niets, om twee redenen die allebei in het
ontwerp zitten en niet in de data.

### Wat er misging

**1. De gezondheidscheck kapt het OOM-pad af**

In `_analyze_component_resources()` (`opi/services/resource_tuning_service.py:215-228`) staat
bovenaan een guard: als de Deployment-conditie `Available != True` is, dan `return None`.

```
08:16:45,177 resource_tuning_service INFO
  Skipping pr-450-api: deployment is not available (reason: MinimumReplicasUnavailable),
  memory data would be misleading
```

Die guard is gebouwd voor de nachtelijke sweep: bij een kapotte deployment is het gemeten
geheugengebruik laag, en zonder guard zou de tuner het limiet ten onrechte verlagen. Maar een
component dat OOM'd staat per definitie op `Available=False`. De guard blokkeert het OOM-pad
dus altijd, precies op het moment dat het moet werken.

Veertig regels verderop staat al de afhandeling die hier bedoeld was, en die vanuit het
OOM-pad onbereikbaar is:

```python
if max_observed_mb == 0:
    if not has_oom_kills:
        return None
    logger.info(f"No memory data for {unique_name} but OOM kills detected, "
                f"using current limits ({current_limit_mb:.0f}Mi) as baseline")
```

Was die bereikt, dan had `compute_memory_recommendation()` met `oom_factor=3.0`
(want `current_limit_mb=45 < 64`) netjes 135Mi voorgesteld.

**2. Het root-component wordt door elke deployment overschreven**

`tune_deployment_resources()` schrijft de nieuwe waarden op deployment-niveau én daarna
onvoorwaardelijk op het root-component (`resource_tuning_service.py:597-611`), zodat nieuwe
deployments "een realistisch startpunt erven". In de praktijk is dat een race waarin de laatste
schrijver wint. Uit `projects/asses-k2n.yaml`:

```yaml
- name: api
  resources:
    limits: { memory: 45Mi }
    history:
      - timestamp: '2026-07-09T23:00:19' limits: {memory: 45Mi} deployment: pr-406
      - timestamp: '2026-07-09T23:00:13' limits: {memory: 75Mi} deployment: productie
```

Binnen zes seconden trok de nachtelijke sweep het gedeelde limiet van 75Mi (gemeten op
`productie`) naar 45Mi (gemeten op het veel lichtere `pr-406`). Elke nieuwe PR-deployment start
sindsdien op 45Mi. `pr-450` heeft een zwaarder image, past er niet in, en OOM't bij het opstarten.

Netto is dit een gesloten lus: de tuner heeft het limiet zelf te krap gezet en kan het daarna
niet meer corrigeren.

## Uitgangspunt

**Een deployment mag zijn eigen geheugen altijd ophogen.** Het root-component is de gedeclareerde
startwaarde, geen gedeelde toestand die de tuner heen en weer trekt. Wat één deployment nodig
heeft is te deployment-afhankelijk om naar de root terug te schrijven.

De benodigde bouwstenen bestaan al:

- Deployment-overrides zijn een bestaande schema-feature
  (`features/futures/configurable-deployment-resources.md`) en de tuner schrijft ze al
  via `set_deployment_component_resources()`.
- `get_resource_history_floor()` (`opi/handlers/project_file_handler.py:1405`) filtert de
  OOM-vloer al per deployment, zodat een OOM in één PR de andere niet vastpint.

## Taken

### 1. Guard richtinggevoelig maken

`opi/services/resource_tuning_service.py`

- Voeg aan `_analyze_component_resources()` een parameter `oom_triggered: bool = False` toe.
- Sla de `Available != True`-guard over als `oom_triggered` waar is. De guard blijft ongewijzigd
  voor de nachtelijke sweep.
- Behoud de bestaande fallback verderop: geen metrics plus OOM betekent het huidige limiet als
  baseline, waarna de sliding `oom_factor` het ophoogt.

Verificatie: unit-test met een Deployment op `MinimumReplicasUnavailable` plus een OOM'ende
container levert een aanbeveling van 3x het huidige limiet, niet `None`.

### 2. Gericht tunen op het component dat OOM'de

`opi/services/resource_tuning_service.py`, `opi/manager/project_manager.py`,
`opi/services/oom_watcher.py`

- `tune_deployment_resources()` krijgt `oom_components: list[str] | None = None`.
- Is die gevuld, dan slaat de componentenlus alle andere componenten over en gaat
  `oom_triggered=True` mee naar de analyse.
- `project_manager.py:3074` geeft de referenties uit `oom_failures` mee (die lijst bestaat daar al).
- `oom_watcher.py:592` verzamelt de `component_ref` per component met `health.oom_detected`
  en geeft die mee.

Dit scheelt ook nutteloos werk: in het pr-450-log gingen drie Prometheus-queries naar
`pr-450-frontend`, dat helemaal niet OOM'de.

Verificatie: in het log verschijnen alleen queries voor het OOM'ende component.

### 3. Auto-tune schrijft niet meer naar het root-component

`opi/services/resource_tuning_service.py:597-611`

- Verwijder de `set_component_resources()`-aanroep en de bijbehorende
  `append_component_resource_history()`.
- Deployment-niveau schrijven en `append_deployment_component_resource_history()` blijven
  ongewijzigd.

Gevolg en afweging: nieuwe deployments starten voortaan op de gedeclareerde root-waarde in plaats
van op wat de laatste tuner-run toevallig schreef. Is die waarde te krap, dan OOM't de eerste
boot één keer en hoogt de watcher hem op, wat na taak 1 en 2 ook echt werkt. Dat kost één
crashcyclus, maar haalt de cross-deployment-race eruit.

Alternatief als die crashcyclus onwenselijk is: root alleen omhoog laten bijstellen, nooit omlaag.
Dat lost de ratchet-down op zonder de erfenis te verliezen, maar laat de root permanent naar de
zwaarste deployment kruipen, inclusief de request die schedulingcapaciteit kost. Voorkeur gaat
uit naar de root helemaal met rust laten.

### 4. Vloer op het gedeclareerde minimum

`opi/services/resource_tuning_service.py`

Na taak 3 is de root de ondergrens die de gebruiker declareerde. Controleer dat een
deployment-override die de tuner schrijft daar niet meer onder kan zakken bij de nachtelijke
sweep, zodat een tijdelijk stille PR-deployment niet onder de gedeclareerde waarde wordt getrokken.
Waarschijnlijk volstaat een `max()` tegen `extract_component_resources()`.

Verificatie: unit-test waarin het gemeten gebruik ver onder het root-request ligt en de override
niet lager uitkomt dan de root.

### 5. History-ruis vreet de OOM-vloer op

`opi/handlers/project_file_handler.py`

De resource-history is niet alleen documentatie. `get_resource_history_floor()` (regel 1441-1459)
leest uitsluitend entries met `source: oom-watcher`, en daarvan alleen de meest recente. Dat is de
preventie die voorkomt dat een component na een OOM weer omlaag getuned wordt. Een
`auto-tune`-entry heeft in die logica geen enkele functie.

Maar de history is gecapt op vijf entries, nieuwste eerst
(`append_component_resource_history`, `max_entries: int = 5`). De auto-tune-ruis duwt de
`oom-watcher`-entries dus actief het venster uit, en daarmee de vloer. Het root-component
`frontend` van asses-k2n laat zien hoe snel dat gaat:

```
23:03:05  pr-405     25Mi  'Limit kept equal at 25Mi'
23:03:03  pr-394     25Mi  'Limit kept equal at 25Mi'
23:03:02  pr-388     25Mi  'Limit kept equal at 25Mi'
23:03:00  pr-354     25Mi  'Limit kept equal at 25Mi'
23:02:55  productie  25Mi  'Limit kept equal at 25Mi'
```

Vijf slots, één sweep, tien seconden. Over de hele projects-repo staan 494 `auto-tune`-entries
tegenover 25 `oom-watcher`-entries. Voor `asses-k2n/api` zijn alle vijf root-slots auto-tune,
dus daar bestaat geen vloer meer.

- Laat het prunen de nieuwste `oom-watcher`-entry altijd behouden, ongeacht de cap. Dat is de
  kleinste ingreep die de vloer weer betrouwbaar maakt.
- Taak 3 haalt de root-duplicatie sowieso weg: nu schrijft elke deployment dezelfde entry naar de
  root, wat vijf slots kost voor één logische wijziging.

Verificatie: unit-test die zes auto-tune-entries toevoegt bovenop een `oom-watcher`-entry en
daarna nog steeds een vloer terugkrijgt uit `get_resource_history_floor()`.

### 6. History legt vast wat er werkelijk wijzigde

`opi/services/resource_tuning_service.py:614-640`

Een entry wordt alleen geschreven als de request óf de limit wijzigde (de `continue` op regel 578
vangt de rest af), maar de entry bevat alleen `limits`. Een wijziging die uitsluitend de request
raakt leest daardoor als een no-op: `'Request: max 3Mi + 25% = 25Mi. Limit kept equal at 25Mi'`
met `limits: {memory: 25Mi}`.

- Neem `requests` op in de history-entry naast `limits`.
- Laat de vloerberekening ongemoeid: die leest `limits.memory` en moet dat blijven doen.

Verificatie: een run waarin alleen de request verandert levert een entry waarin dat verschil
zichtbaar is.

### 7. De health-service ruimt bestaande ruis zelf op

`opi/handlers/project_file_handler.py`, `opi/services/resource_tuning_service.py`

Taak 5 en 6 stoppen de aanwas, maar de vensters die al volgelopen zijn blijven staan. Het
opruimen daarvan hoort bij de service die deze data toch al inleest en beheert, niet bij een
losse migratie.

- Nieuwe helper `compact_resource_history(project_data) -> bool` in `project_file_handler.py`,
  waar de andere history-helpers ook staan. Regels, in volgorde:
  1. Behoud altijd de nieuwste `oom-watcher`-entry (dezelfde regel als taak 5, deel de helper).
  2. Vouw een reeks opeenvolgende `auto-tune`-entries met identieke waarden samen tot de nieuwste.
  3. Pas daarna de cap toe.
- Aanroepen in `tune_deployment_resources()` vlak vóór `save_and_commit_project()`, zodat het
  meelift op een commit die er toch al komt.

**Geen eigen commit en geen vlootbrede herschrijving.** Dat is een bewuste keuze: het
projectbestand herschrijven kost SOPS- en AGE-churn, en dit is hygiëne, geen correctheidsfix.
Na taak 5 overleeft de vloer een volgelopen venster sowieso, dus er is geen reden om projecten
aan te raken die verder niets te wijzigen hebben. Ze worden vanzelf opgeschoond zodra de tuner
er de eerstvolgende keer een echte wijziging voor commit.

Wat het niet doet: een vloer die al uit het venster is geduwd komt niet terug. Die wordt gewoon
opnieuw gezet zodra het component weer OOM't.

Verificatie: unit-test met de vijf identieke `pr-405`- tot `productie`-entries uit asses-k2n
plus een oudere `oom-watcher`-entry. Verwachting: één auto-tune-entry over, `oom-watcher`-entry
behouden, en `get_resource_history_floor()` geeft weer een vloer terug.

### 8. Regressietest op het volledige pad

`operations-manager/python/tests/`

Test die het pr-450-scenario nabootst: component met 45Mi limiet, Deployment op
`MinimumReplicasUnavailable`, geen Prometheus-data, OOM gedetecteerd. Verwachting: een
deployment-override van 135Mi, een `oom-watcher`-history-entry op deployment-niveau, en een
ongewijzigd root-component.

## Verificatie end-to-end

1. `cd operations-manager/python && uv run pytest tests/ -k "resource_tuning or oom or history" -q`
2. `uv run ruff check . --fix && uv run ruff format . && uv run pyright`
3. In de sandbox: een component met een bewust te laag geheugenlimiet deployen, en in
   `kubectl logs -n rig-system deployment/operations-manager -f` volgen dat de auto-tune
   het deployment-override ophoogt en een refresh queuet, in plaats van
   "found no actionable changes" te loggen.
4. Controleer in de projects-repo dat de commit alleen het deployment-blok raakt en het
   root-component ongemoeid laat.

## Losse einden

- **Richting: health als eigenstandige interne service.** De OOM-afhandeling zit nu verspreid
  over `oom_watcher.py` (fire-and-forget na deploy), de inline-detectie in `project_manager.py`
  en de geheugencheck op de projectpagina. Taak 2 en 7 trekken verantwoordelijkheden alvast naar
  één plek: wie de OOM ziet, bepaalt wat er getuned wordt en houdt de bijbehorende data schoon.
  Let op de terminologie: dit is een interne service onder `opi/services/`, naast
  `resource_tuning_service` en `project_service`. Het is géén catalogusservice, want
  `opi/services/catalog/` is voor bouwstenen die een project zelf aanzet in zijn projectbestand
  (zie `instructions/services.md`). De bredere samentrekking staat in
  `features/futures/system-wide-oom-watcher.md` en valt buiten dit plan.
- **pr-450 zit nu vast.** De codefix staat niet in productie, dus deze deployment komt er niet
  vanzelf uit. Handmatig herstel: een `resources`-override op `deployments[pr-450].components[api]`
  met minstens 135Mi, of de root terug naar 75Mi. Dat is een aparte, bewuste actie.
- **Silent failure.** Dit past in het bekende patroon dat mislukte reprocessing en validatie
  alleen in het log landen. Alarmering hierop is een eigen traject.
- **Systeembrede watcher.** `features/futures/system-wide-oom-watcher.md` beschrijft OOM's die
  ná een geslaagde deploy ontstaan. Andere scope, geen overlap met dit plan.
