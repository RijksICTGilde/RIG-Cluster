# Een handmatig gezette resource-waarde wint van de auto-tuner

> Uitgevoerd in RC-141, PR #137, tak `een-handmatig-gezette-resource-waarde-wint-van-de`.
> Afwijkingen van het plan staan onderaan.

## Aanleiding

Op `mpfb-8wh` wijzigde een gebruiker op 19-08 om 12:59 via de portal de CPU van component
`logius-fscoutway` (commit `3ab031de4`: request `32m` -> `50m`, limit `200m` -> `'1'`). Die
wijziging heeft nooit gewerkt. De pods draaien nog steeds op 32m/1000m.

Oorzaak: er zijn twee schrijvers die elk naar een ander niveau schrijven, en het niveau dat
de gebruiker NIET gebruikt wint bij manifestgeneratie.

| schrijver | schrijft naar | code |
|---|---|---|
| portal "Component bewerken" + API `update_component`/`add_component` | root `components[]` (de catalogus) | `FlowTarget("components", i)` in `opi/forms/visualizers/flows.py:387`, `apply_resource_limits` via `project_manager.py:7817` en `project_utils.py:279` |
| auto-tuner (nachtelijk, VPA) | `deployments[].components[].resources` | `set_deployment_component_resources`, `resource_tuning_service.py:671` |

`project_manager.py:5535-5543` merget per veld, waarbij het deployment-niveau wint:

```python
component_resources = ...extract_component_resources(project_data, component_reference)
deployment_resources = ...extract_deployment_component_resources(project_data, deployment_name, component_reference)
if deployment_resources:
    component_resources.update(deployment_resources)
```

Zodra de tuner een component ooit heeft aangeraakt, is elke resource-bewerking via portal of
API voor dat component dus een stille no-op. `set_component_resources` (de catalogus-setter in
`ProjectFileHandler`) heeft vandaag nul aanroepers.

## Uitgangspunt

De catalogus is de wens van de gebruiker. De deployment-override is de werkkopie van de tuner.
De tuner is dienstig: hij mag corrigeren, maar hij mag een gebruiker niet stilzwijgend
overrulen. Andersom moet een gebruikerskeuze niet eeuwig blijven staan, want dan kan de tuner
een veel te ruim gezette waarde (iemand zet 4Gi op iets dat 100Mi gebruikt) nooit meer
rechtzetten.

## Aanpak

Geen schemawijziging. Het mechanisme bestaat al voor OOM en wordt hergebruikt:

- `get_resource_history_floor` (`project_file_handler.py:1504`) leest de nieuwste
  `oom-watcher`-entry uit `resources.history` en gebruikt die als vloer.
- `_prune_resource_history` (`project_file_handler.py:252`) houdt de nieuwste
  `oom-watcher`-entry altijd vast, ook als het venster (`max_entries=5`) vol loopt.
- `_floor_is_expired` (`resource_tuning_service.py:175`) laat de vloer vervallen zodra de
  entry oud genoeg is EN het component sindsdien ruim onder de vloer draait.

`source` in `$defs/resource-history-entry` (`opi/schemas/project_v2.json:231`) heeft de waarde
`"manual"` al in de enum en niemand schrijft hem vandaag. Dat wordt de drager van de
gebruikersintentie. Dus: puur logica, geen schemawijziging, geen migratie.

Eén regel voor CPU en geheugen, langs hetzelfde codepad en dezelfde vervalregel. Geen aparte
CPU-behandeling.

## Taken

### 1. Eén gedeeld schrijfpad voor een gebruikerswens

Nieuw in `ProjectFileHandler`:

```python
def apply_user_resource_intent(
    self,
    project_data: dict[str, Any],
    component_name: str,
    resources: dict[str, str],   # vlakke sleutels: requests_cpu / requests_memory / limits_cpu / limits_memory, partieel
    origin: str,                 # "portal" of "api", belandt in de reason van de history-entry
) -> list[str]:                  # de velden die daadwerkelijk wijzigden
```

Gedrag, in deze volgorde:

1. Lees de huidige catalogus-waarden van het component via `_parse_resources_block_partial`
   (dus zonder defaults in te vullen).
2. Bepaal de gewijzigde velden: alleen velden die in `resources` zitten EN afwijken van de
   huidige catalogus-waarde. **Dit is essentieel**: de modal post altijd alle vier de velden,
   dus zonder deze diff zou elke willekeurige component-edit een gebruikersintentie op alle
   vier de velden vastleggen en de tuner volledig lamleggen.
3. Zijn er geen gewijzigde velden, doe dan niets en geef `[]` terug. Geen history-entry, geen
   commit-ruis.
4. Schrijf de gewijzigde velden in de catalogus-component via `_apply_flat_resources`.
5. Verwijder **precies die velden** uit de `resources` van elke deployment-component die naar
   dit component verwijst. Dus niet het hele override-blok weggooien: een CPU-edit mag de door
   de tuner (of de OOM-watcher) gezette geheugenwaarde van die deployment niet meesleuren.
   Laat lege `requests`/`limits`-dicts opruimen, maar laat `history` staan.
6. Voeg één entry toe aan de history van de **catalogus**-component:
   `{"timestamp": <UTC iso>, "source": "manual", "requests": {...}, "limits": {...}, "reason": "..."}`,
   alleen met de gewijzigde velden, en **zonder** `deployment`-veld. In de bestaande conventie
   van `get_resource_history_floor` betekent een ontbrekend `deployment`-veld "geldt voor alle
   deployments", precies wat hier bedoeld is.

`apply_resource_limits` (`project_utils.py:173`) blijft bestaan als de lage nested-vorm-writer
en wordt vanuit deze functie gebruikt, maar krijgt geen aanroepers meer daarbuiten.

### 2. Alle schrijvers door dat ene pad

De user-facing schrijvers moeten allemaal via `apply_user_resource_intent`, geen tweede
implementatie ernaast:

- `opi/forms/wizard/save.py`: de component-edit-flow (`flow.target.list_key == "components"`).
  Het punt om in te haken is na de merge en vóór `save_and_commit_project`, zodat de functie de
  oude waarde nog kan zien voor de diff uit stap 1.2. Let op: de merge schrijft de nieuwe
  waarde al in `existing_data`, dus de vorige waarde moet vóór `apply_write_paths` worden
  vastgehouden, of de functie moet de vorige waarde meekrijgen.
- `project_manager.update_component` (`project_manager.py:7815`): vervang de directe
  `apply_resource_limits(resources, ...)` door de gedeelde functie.
- `project_manager.add_component` -> `build_component_config` (`project_utils.py:279`): een
  nieuw component heeft nog geen overrides en geen history, dus stap 5 is een no-op, maar route
  hem toch via dezelfde functie zodat er één schrijver is.

Voeg een test toe die borgt dat dit zo blijft: grep-guard in de trant van de bestaande
single-path-guard, die faalt zodra `apply_resource_limits` of `set_component_resources` buiten
`apply_user_resource_intent` wordt aangeroepen.

Buiten scope: de API kent alleen `cpu_limit`/`memory_limit` en geen requests. Dat blijft zo;
de gedeelde functie accepteert partiële invoer.

### 3. Snoeien mag de intentie niet weggooien

`_prune_resource_history` (`project_file_handler.py:252`) beschermt nu alleen de nieuwste
`oom-watcher`-entry. Breid uit naar twee beschermde bronnen: `oom-watcher` én `manual`, allebei
alleen de nieuwste. Houd `max_entries` hard: ontbreekt een beschermde entry in het venster, dan
vervangt hij de oudste niet-beschermde entry. Werken beide beschermde entries niet in het
venster, dan vervangen ze de twee oudste niet-beschermde entries.

`_compact_resource_history_list` vouwt alleen runs van identieke `auto-tune`-entries, dus
`manual`-entries breken een run en blijven vanzelf staan. Wel verifiëren.

### 4. De tuner respecteert een levende intentie

Nieuw in `ProjectFileHandler`, gemodelleerd naar `get_resource_history_floor`:

```python
def get_user_resource_intent(
    self, project_data, deployment_name, component_reference
) -> UserResourceIntent | None
```

Leest de nieuwste `manual`-entry uit de catalogus-history van het component (en van de
deployment-component, voor symmetrie), gefilterd op het `deployment`-veld zoals de OOM-vloer
dat doet: geen `deployment` betekent "geldt voor alle deployments". Geeft de gezette velden en
de `timestamp` terug.

In `_analyze_component_resources` (`resource_tuning_service.py:201`):

- Haal de intentie op. Vervallen bepalen met een regel die `_floor_is_expired` spiegelt: de
  entry is ouder dan `user_intent_min_age_days` EN het gemeten gebruik ligt ruim onder de
  gezette waarde (`user_intent_stable_percent`). Voor geheugen is "gemeten gebruik"
  `max_observed_mb`, voor CPU de VPA-target. Eén regel, twee metrieken.
- Zolang de intentie leeft, slaat de tuner **exact de genoemde velden** over en tunet hij de
  rest gewoon door. Log per overgeslagen veld op INFO waarom, met de timestamp van de entry,
  zodat dit terug te zien is in Loki.
- **Eén bewuste uitzondering**: een actieve OOM (`has_oom_kills`) mag de geheugenlimiet ook
  boven een levende gebruikersintentie tillen. Een pod die op dit moment OOM-killed wordt is
  precies het geval waarin de tuner moet ingrijpen. De bestaande OOM-noodroute
  (`resource_tuning_service.py:423-468`) blijft daarmee volledig intact. Leg dit vast in de
  docstring, want het is de enige plek waar de tuner de gebruiker overruled.

Config in `opi/services/catalog/resource_tuning/config.py` + `config_model.py`, naast de
bestaande OOM-velden:

```python
"user_intent_min_age_days": 10,
"user_intent_stable_percent": 50,
```

Startwaarden gelijk aan de OOM-vloer, zodat het gedrag uit te leggen is; ze staan los zodat ze
apart bij te draaien zijn.

### 5. De `limit_frozen`-heuristiek wordt fallback, geen bron van waarheid

`compute_cpu_recommendation` (`resource_analyzer.py:167-171`) leidt intentie vandaag af uit
`current_limit_m != current_request_m`:

```python
limit_frozen = current_limit_m != current_request_m
recommended_limit_m = current_limit_m if limit_frozen else recommended_request_m
```

Dat raadt, en het raadt in twee richtingen fout: een door de tuner gezet paar dat toevallig
verschilt is óók bevroren, en een gebruiker die bewust limit gelijk aan request zet verliest
zijn bescherming.

**Afwijking van het oorspronkelijke voorstel, bewust**: de heuristiek wordt NIET verwijderd.
Blunt weghalen laat de tuner de limit gelijktrekken met de request voor elk component dat nu
limit != request heeft, en dat is in productie zo goed als alles (bijvoorbeeld de hele
FSC-set op request 32m / limit 1000m). Die limits zouden in één nachtelijke sweep naar ~31m
zakken en de boel hard gaan throttlen. Dat is een zware regressie voor een cosmetische
opruiming.

Wat er wel gebeurt: een vastgelegde `manual`-intentie gaat vóór de heuristiek. Bestaat er een
levende intentie op `limits.cpu`, dan slaat stap 4 het veld sowieso over en komt de heuristiek
niet aan bod. Bestaat die niet, dan blijft de bestaande freeze het gedrag bepalen. Documenteer
in de docstring dat de heuristiek de fallback is voor componenten zonder vastgelegde intentie,
en dat hij kan vervallen zodra de intentie breed is vastgelegd.

## Verificatie

Per taak, allemaal als pytest onder `operations-manager/python/tests/`:

1. Component met tuner-overrides in twee deployments; een CPU-edit op de catalogus levert in
   beide deployments de nieuwe CPU op bij manifestgeneratie, terwijl de getunede
   geheugenwaarde van die deployments ongemoeid blijft.
2. Een edit die niets wijzigt (dezelfde waarden opnieuw posten) schrijft geen history-entry en
   verandert niets.
3. Grep-guard: `apply_resource_limits` en `set_component_resources` worden alleen vanuit
   `apply_user_resource_intent` aangeroepen.
4. Een history met vijf `auto-tune`-entries plus één oudere `manual` en één oudere
   `oom-watcher` houdt na snoeien beide beschermde entries en telt niet meer dan `max_entries`.
5. Tuner-sweep over een component met een levende `manual`-CPU: CPU blijft ongemoeid, geheugen
   wordt gewoon getuned.
6. Tuner-sweep over een component met een vervallen `manual` (oud genoeg, gebruik ruim
   eronder): de tuner pakt het veld weer op. Gebruik `freezegun` zoals de bestaande
   tuner-tests.
7. Tuner-sweep over een component met een levende `manual`-geheugenwaarde EN actieve
   OOM-kills: de limit gaat wél omhoog.

Daarnaast:

```bash
cd operations-manager/python
uv run pytest tests/ -k "resource or tuning or history" -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

## Buiten scope

- De API uitbreiden met requests naast limits.
- Terugwerkend intentie afleiden voor waarden die vóór deze wijziging handmatig zijn gezet. Die
  hebben geen `manual`-entry en krijgen dus pas bescherming bij de volgende edit. Bewuste
  keuze: raden naar historische intentie is precies waar taak 5 vanaf wil.
- Het repareren van `mpfb-8wh` zelf. Dat is een losse actie zodra dit draait.
- UI die toont dat een waarde vaststaat en een knop om de intentie los te laten. Wenselijk,
  maar apart; zonder die knop is een intentie alleen kwijt te raken door hem te overschrijven
  of hem te laten vervallen.

---

## Wat er bij de uitvoering afweek

- **`apply_user_resource_intent` kreeg een `previous`-parameter.** De portalweg merget de
  nieuwe waarden al in `existing_data` voordat er iets te vergelijken valt, dus `save.py`
  houdt de oude waarden vast vóór `apply_write_paths` en geeft ze mee. Zonder parameter zou
  de diff tegen de al bijgewerkte catalogus lopen en dus altijd leeg zijn.
- **Het schrijven loopt via `set_component_resources`** (dat zelf `_apply_flat_resources`
  gebruikt) in plaats van rechtstreeks. Zo heeft die zetter één echte aanroeper en bewaakt de
  grendel uit taak 2 iets in plaats van niets.
- **Het geknepen geheugen-request telt niet als wens.** Verlaagt een limiet het bestaande
  request (het gedrag van `apply_resource_limits`), dan wordt die waarde wel geschreven maar
  niet als `manual` vastgelegd: een afgeleide waarde is geen wens, en vastzetten zou de tuner
  van een request afhouden die niemand koos.
- **Invarianten na het terugzetten.** Zet je maar één helft van een paar vast, dan kan het
  request boven zijn limiet uitkomen. Wat NIET vastgezet is geeft mee: bij een vastgezette
  limiet wordt het request geknepen, bij een vastgezet request gaat de limiet omhoog. Beide
  vastgezet: onaangeroerd.
- **Geen `freezegun` nodig** voor verificatie 6: een tijdstempel van `now - 30 dagen` is
  deterministisch en leest makkelijker dan een bevroren klok.
- **`build_component_config` importeert `ProjectFileHandler` binnen de functie.** De handler
  importeert deze module voor `apply_resource_limits`; een import bovenaan zou een cyclus
  geven. Dezelfde reden die de moduledocstring van `project_utils` al noemt.
- **Twee formuliertests moesten mee.** `test_flow_write_isolation.py` en
  `test_helmfile_blijft_behouden.py` vergelijken byte voor byte. In plaats van de vergelijking
  op te rekken verantwoordt één helper wat een resource-bewerking bewust extra doet: het
  `manual`-item, de migratie van een legacy platte `cpu`-sleutel naar `limits.cpu`, en het
  meetrekken van een ontbrekend geheugen-request.
- **De sectiestroom `modal-edit-components` blijft buiten schot**: die heeft geen `target`,
  bewerkt de hele lijst en is niet vanuit het scherm te openen.
- **Sandboxvalidatie kon niet.** Een verse pod blijft 0/1 op de Keycloak-readinessmeting.
  Niet door een verlopen certificaat -- dat was de eerste, foute diagnose -- maar door de
  truststore van de pod: `REQUESTS_CA_BUNDLE` en `SSL_CERT_FILE` wijzen allebei naar
  `/etc/rig-ca/rig-sandbox-dev-ca.crt`, dat alleen de zelfondertekende RIG Sandbox Dev CA
  bevat, terwijl de ingress een Let's Encrypt-certificaat serveert. Vandaar de KETENfout
  `unable to get local issuer certificate`. Gemeld op de PR, lock direct weer vrijgegeven.

## Reparaties na de review (r1)

- **Het nieuwste `manual`-item draagt de VOLLEDIGE staande wens.** De lezer neemt per niveau
  precies het nieuwste item, dus een item met alleen de velden van die ene bewerking liet elke
  tweede bewerking de eerste wens stil weggooien -- de gewone flow, niet een randgeval. Elk
  nieuw item neemt nu de nog staande velden van het vorige item mee (velden die deze bewerking
  niet overschrijft en waarvan de waarde nog in de catalogus staat), inclusief het opruimen van
  hun deployment-override. Een meegenomen veld krijgt de nieuwe tijdstempel, dus zijn
  vervaltermijn gaat opnieuw lopen; dat staat in `features/handmatig-gezette-resources.md`.
- **`_prune_resource_history` reserveert de slots vooraf** in plaats van achteraf een
  niet-beschermd item te vervangen. In een venster dat volledig uit beschermde items bestaat
  (een OOM-storm) vond die vervanging geen vrij slot en viel de wens alsnog weg.
- **De sandboxdiagnose is gecorrigeerd** (zie hierboven): het certificaat in het cluster is
  geldig tot 16-11-2026; de eerste meting keek naar poort 443, en dat is de Caddy-rand.

## Reparaties na de review (r2)

- **Een resourcepaar gaat altijd in zijn geheel uit de deployment-override.** Manifestgeneratie
  merget per veld, dus alleen `limits_memory` opruimen liet het `requests_memory` van de tuner
  staan en kon `requests > limits` opleveren -- een Deployment die de apiserver weigert, precies
  na de bewerking waar de gebruiker op wacht. Na het opruimen wordt het gemergde paar getoetst
  en gaat de andere helft er zo nodig ook uit.
- **Het opruimen van de staande wens laat de OOM-verhoging staan.** Een geheugenlimiet die de
  OOM-watcher boven de wens heeft getild is de ene plek waar het platform de gebruiker bewust
  overruled; die override wegvegen op een niet-gerelateerde CPU-bewerking liet de pod terugvallen
  op de limiet die hem eerder omver duwde.

## Reparaties na de beveiligingsreview

- **De platformcap blijft gelden op de bijwerkweg van de API.** `UPDATE_COMPONENT_VALIDATORS`
  toetste `cpu_limit` en `memory_limit` niet, terwijl `ADD_COMPONENT_VALIDATORS` dat wel deed;
  `PATCH .../components/{c}` met `{"memory_limit": "64Gi"}` ging er dus doorheen. Dat gat is
  voorbestaand, maar dit plan haalt de correctie erop weg: tot nu toe klemde de tuner zo'n
  waarde bij de eerstvolgende sweep terug, en na deze wijziging laat hij een vastgelegde wens
  juist met rust. De vervalregel redt het niet -- die eist ouderdom **en** gebruik onder de
  helft van de gezette waarde, dus een workload die zijn eigen pin vol houdt, houdt hem
  onbeperkt. Er is ook geen derde lijn: geen `ResourceQuota` of `LimitRange` in `manifests/`,
  en `resources` is in het schema kaal `type: string`.

  Gekozen reparatie: de twee limieten toevoegen aan `UPDATE_COMPONENT_VALIDATORS`, met exact
  dezelfde twee editables die de aanmaakweg al gebruikt. Dat dekt v1 en v2 (beide routers
  draaien dat profiel), weigert met dezelfde melding als het formulier in plaats van de waarde
  stil te klemmen, en haalt de divergentie tussen het ADD- en het UPDATE-profiel weg in plaats
  van er een tweede regel naast te zetten. Het alternatief uit de review (klemmen in
  `apply_user_resource_intent`) zou de gebruiker een andere waarde geven dan hij vroeg, zonder
  dat iets dat vertelt.

  **Zichtbaar gevolg**: een `PATCH` die de huidige `cpu_limit` van een component teruggeeft
  terwijl die niet `500m` of `1` is (in productie komt dat voor, bijvoorbeeld `200m`) krijgt nu
  een 422. Dat is hetzelfde antwoord dat de aanmaakweg en het formulier al gaven; de waarde
  hoefde alleen niet meegestuurd te worden, want een PATCH draagt alleen wat hij wijzigt.

## Reparaties na de review (r3)

- **De aanmaakweg legt geen wens meer vast.** Taak 2 routeerde `build_component_config` door
  `apply_user_resource_intent` "zodat er één schrijver is". Dat is de verkeerde afweging
  gebleken: een aanmaakverzoek draagt ALTIJD twee limieten (de wizard vult 1 CPU / 256Mi voor,
  de API valt op dezelfde waarden terug), dus elk nieuw component kreeg meteen een
  `manual`-item. Gevolg, gemeten met de echte functies: een gezond component (200Mi
  waargenomen, geen OOM) waarvoor de tuner 512Mi/300Mi wil, wordt teruggezet op 256Mi met
  `request == limit` -- zonder piekruimte, precies waar `min_limit_headroom_mi` voor is. De
  vervalregel redt dat niet (ouderdom EN gebruik onder de helft), dus alles boven 128Mi houdt
  zijn 256Mi onbeperkt; alleen een echte OOM-kill komt er nog uit. Dat gold voor 100% van de
  nieuw aangemaakte componenten, op de API-weg en op de zelfbedieningswizard.

  De aanmaakweg schrijft de limieten daarom weer rechtstreeks met `apply_resource_limits`
  (het gedrag van vóór deze tak). Een voorgevulde standaard die niemand koos is geen wens; de
  eerste echte bewerking is dat wel en die loopt nog steeds via het gedeelde pad. Er gaat bij
  het rechtstreeks schrijven niets verloren: een component dat nog niet bestaat heeft geen
  override om op te ruimen en geen historie om mee te dragen. De AST-grendel noemt
  `build_component_config` nu expliciet als de enige andere toegestane aanroeper van
  `apply_resource_limits`; `set_component_resources` blijft op één aanroeper staan.

- **De parameter `origin` op `build_component_config` is weg.** Geen enkele aanroeper gaf hem
  mee, dus de wizardweg schreef "Set by hand via api" in de reason van een portaalproject. Nu
  de aanmaakweg geen historie-item meer schrijft heeft de parameter geen doel (YAGNI).
