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

### Invariant: root mag een deployment nooit beletten zichzelf op te hogen

Dit is de harde regel waar elke keuze hieronder aan getoetst wordt, ook de nog open keuze in
taak 3.

Vandaag geldt hij al, en dat is nagetrokken:
`extract_deployment_component_resources()` (`project_file_handler.py:1193`) geeft alleen terug
wat expliciet op de deployment staat, en de samenvoeging is
`current_resources.update(deployment_overrides)`. De deployment wint dus altijd. Het enige
echte plafond is de clusterlimiet `max_memory_limit_mi` (4096Mi), niet de root.

Wat `pr-450` dan wél vastzette: root was de *bron* van de te krappe 45Mi, terwijl de
availability-guard tegelijk het pad dichtzette waarlangs de deployment zichzelf had kunnen
ophogen. Root capte niets, maar leverde de krappe startwaarde op een moment dat ontsnappen
onmogelijk was. Taak 1 herstelt die ontsnapping; taak 3 gaat over de startwaarde.

Let op bij taak 4: die gebruikt root als *ondergrens*. Dat is de andere richting en botst niet
met deze invariant, maar het is wel de plek waar per ongeluk een plafond kan ontstaan.

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

### 3. Wat auto-tune met het root-component doet (OPEN, niet implementeren)

`opi/services/resource_tuning_service.py:597-611`

Vaststaand: de onvoorwaardelijke ratchet-down moet weg. Nu schrijft elke deployment het
gedeelde root-limiet, en dat trok `asses-k2n/api` binnen zes seconden van 75Mi naar 45Mi.

Nog niet beslist: wat er daarvoor in de plaats komt. Twee routes, allebei voldoen ze aan de
invariant hierboven, dus dit gaat puur over de startwaarde die een níeuwe deployment erft.

**A. Root helemaal met rust laten.** Verwijder de `set_component_resources()`-aanroep en de
bijbehorende `append_component_resource_history()`; alleen deployment-overrides blijven.
Root is dan wat de gebruiker declareerde, punt. Kost: een nieuwe deployment die te krap start
OOM't één keer voordat de watcher hem ophoogt.

**B. Root alleen omhoog.** Nooit meer verlagen, wel verhogen naar de zwaarste deployment.
Geen ratchet-down, en nieuwe deployments starten ruim. Kost: root kruipt permanent omhoog,
inclusief de request, en die vraagt wel echte schedulingcapaciteit.

Wat de keuze scherper zou maken en nu ontbreekt: hoe vaak een nieuwe deployment daadwerkelijk
te krap zou starten. Dat is te meten aan de bestaande resource-history voordat we kiezen.

Taak 4 hangt hieraan: die gebruikt root als ondergrens, wat bij route B een heel andere
strengheid krijgt dan bij route A.

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

### 7. De resource-tuning-service ruimt bestaande ruis zelf op

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

### 8. Hookpunt: van inline-branching naar een gescande service

`opi/services/catalog/base.py`, `opi/services/registry.py`, `opi/services/services.py`

Vandaag staat de OOM-afhandeling hard in de generieke deploycode
(`project_manager.py:3049-3100`): failures categoriseren, `if oom_failures:` tunen,
`if image_pull_failures:` componenten uitzetten. Dat is precies wat
`instructions/services.md` verbiedt: generieke code die per concern vertakt in plaats van
de registry te itereren.

De registry heeft dit patroon al, één scanfunctie per hookpunt:

```python
def provisioning_services() -> list[Service]:
    overriding = [s for s in SERVICES.values() if type(s).provision is not Service.provision]
    return sorted(overriding, key=lambda s: s.provision_order)
```

Wat ontbreekt is een hookpunt ná de sync. De bestaande hooks (`provision`,
`contribute_manifest_context`, `config_editables`, `config_approvals`) zitten allemaal op
definitie- en generatietijd. Deze service moet juist de *draaiende* toestand waarnemen en
mag daarop het projectbestand wijzigen.

**Hookpunten zijn een enum, nooit strings.** In `opi/services/services_enums.py`, naast
`ServiceType`. Een kale `Enum`, geen `StrEnum`, precies zoals `ServiceType` en `ConfigLayer`:
daardoor is `hook == "after-sync"` simpelweg `False` en kan een losse string nergens
binnensluipen.

```python
class HookLevel(Enum):
    """Waar een hookpunt over itereert."""

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    COMPONENT = "component"


class HookPoint(Enum):
    """Moment in de deploy-levenscyclus waarop generieke code de registry scant."""

    AFTER_SYNC = "after-sync"

    @property
    def level(self) -> HookLevel:
        return _HOOK_LEVELS[self]
```

Eén member nu, want dat is wat we nodig hebben. `AFTER_SYNC` is deployment-niveau: hij vuurt
één keer per deployment, na de sync. Het niveau benoemen kost een regel en maakt straks
duidelijk wat generieke code moet itereren.

**Bouw de project- en componentiteratie nog niet.** `HookLevel` benoemt de as, maar zolang er
alleen een deployment-hook is zou machinerie voor de andere twee niveaus code zijn zonder
aanroeper. Die komt erbij op het moment dat een hook hem nodig heeft.

**Nieuw hookpunt.** Op `Service` een no-op default, zodat alleen wie hem overschrijft
gescand wordt:

```python
async def observe_deployment(self, ctx: DeploymentObservationContext) -> ObservationOutcome:
    return ObservationOutcome()
```

**Deelname wordt afgeleid, niet gedeclareerd.** Een service die zowel een lijst hookpunten
opschrijft als de methode implementeert kan uit elkaar lopen. Leid het dus af uit de
override, met de enum als sleutel:

```python
_HOOK_DEFAULTS: dict[HookPoint, Any] = {
    HookPoint.AFTER_SYNC: Service.observe_deployment,
}

def services_for_hook(hook: HookPoint) -> list[Service]:
    """Services die op dit hookpunt meedoen, in hun volgorde voor dat punt."""
    default = _HOOK_DEFAULTS[hook]
    overriding = [s for s in SERVICES.values()
                  if getattr(type(s), default.__name__) is not default]
    return sorted(overriding, key=lambda s: s.hook_order.get(hook, 100))
```

Dit is hetzelfde override-detectiepatroon als `provisioning_services()`, alleen met een
enum-sleutel ervoor. Volgorde staat per hookpunt in `hook_order: dict[HookPoint, int]`,
zodat een service die straks op twee punten meedoet niet één gedeelde `*_order` hoeft te
delen.

Dezelfde regel geldt voor de bestaande stringly-typed velden op services. Die gaan in taak 10
mee om.

**`DeploymentObservationContext`**, in lijn met `ProvisionContext` en `ManifestContext`:
`project_name`, `deployment_name`, `project_data` (in-memory, muteerbaar), `namespace`,
`cluster`, de waargenomen pod-health per component (oom, crash_loop, image_pull) en
`get_manager(key)`. Een service praat nooit zelf met kubectl, precies zoals nu.

**`ObservationOutcome`**, declaratief zoals `ManifestContribution`:

| Veld | Betekenis |
|---|---|
| `project_data_changed` | De service heeft `ctx.project_data` gewijzigd |
| `requeue_refresh` | Er moet een refresh-taak in de wachtrij |
| `failures` | Meldingen die als sync-failure naar boven komen |
| `notices` | Niet-blokkerende meldingen voor de taakvoortgang |

**Het belangrijkste contract: een hook committeert niet zelf.** Nu laadt
`tune_deployment_resources()` het projectbestand opnieuw uit git en roept zelf
`save_and_commit_project()` aan. Zodra er een tweede service op ditzelfde hookpunt hangt
levert dat twee commits en een lost-update-race op. De hook muteert dus alleen
`ctx.project_data`; de generieke aanroeper doet ná de scan één
`save_and_commit_project()` voor alle uitkomsten samen. Dat houdt ook het bestaande
single-save-path in stand.

Verificatie: unit-test met twee dummy-services op het hookpunt die allebei `project_data`
wijzigen, en precies één commit als resultaat.

### 9. Servicesoort: system naast user

`opi/services/services_enums.py`, `opi/services/services.py`, `opi/services/registry.py`,
`opi/forms/visualizers/providers.py`

De resource-tuning-service werkt als elke andere service, maar een gebruiker kan hem niet aan- of
uitzetten. Dat vraagt om een soort-markering.

**Op de service, niet in de clusterconfig.** Een lijstje "services die altijd draaien" in de
clusterconfig splitst de declaratie over twee plekken, terwijl `instructions/services.md`
juist eist dat een service alles over zichzelf declareert. Bovendien is "systeemdienst" geen
clustereigenschap: de clusterconfig gaat over waar clusters echt van elkaar verschillen
(geheugenplafonds, VPA-ondersteuning). Een per-cluster lijst zou drift mogelijk maken in iets
dat overal hetzelfde hoort te zijn.

```python
class ServiceKind(Enum):
    """Of een project deze service zelf kiest, of dat het platform hem altijd draait."""

    USER = "user"      # staat in de services-lijst van het projectbestand
    SYSTEM = "system"  # draait altijd, staat nooit in de lijst
```

Veld op `ServiceDefinition`: `kind: ServiceKind = ServiceKind.USER`. Alleen de resource-tuning-service
krijgt `SYSTEM`, alle bestaande services houden de default.

**`hidden` blijft bestaan en betekent iets anders.** Nu gebruiken drie services `hidden=True`
met twee betekenissen: `NAMESPACE_POSTGRESQL_DATABASE` en `NAMESPACE_REDIS` zijn gewone
gebruikersservices die alleen niet los kiesbaar zijn (OPI kiest de variant op basis van het
cluster), en `PLATFORM` is altijd aan. `hidden` zegt dus alleen "niet in de kiezer" en niets
over altijd-aan. Na deze taak:

- namespace-varianten: `kind=USER`, `hidden=True` (ongewijzigd gedrag)
- `PLATFORM`: `kind=SYSTEM`, en `hidden` kan daar weg
- de filter op `providers.py:117` wordt `if definition.hidden or definition.kind is ServiceKind.SYSTEM`

**Scan bij projectverwerking.** De hookscan uit taak 8 levert kandidaten; welke daarvan op
dít project van toepassing zijn is een tweede vraag. Zet dat antwoord op de service, met de
soort als default, zodat generieke code nergens op selectie hoeft te vertakken:

```python
def applies_to(self, project_data: dict, deployment_name: str) -> bool:
    """System draait altijd; user alleen als het project de service gekozen heeft."""
```

De aanroeper op het hookpunt wordt daarmee: scan de registry voor `HookPoint.AFTER_SYNC`,
filter op `applies_to`, roep aan in `hook_order`. Geen enkele plek noemt de resource-tuning-service
bij naam.

**Migratie system naar user is voor later, en is minder eng dan het lijkt.** Als een
systeemdienst ooit een gebruikersservice wordt, missen bestaande projecten de entry in hun
services-lijst. Dat is een gewone schemamigratie die de entry toevoegt, en het
migratiemechanisme bestaat al (`migrate_to_latest`, `_fixup_v2_data`). Het lastige is niet de
data maar de beslissing wie hem aan houdt, en die kun je pas nemen als het zover is. De
omgekeerde richting is triviaal: entries worden overbodig en de migratie haalt ze weg.

Verificatie: unit-test dat `applies_to` voor de resource-tuning-service `True` geeft op een project dat
geen enkele service selecteert, en dat de servicekiezer in de wizard hem niet toont.

### 10. De resource-tuning-service registreren en beide aanroepers erop aansluiten

`opi/services/catalog/resource_tuning/`, `opi/manager/project_manager.py`, `opi/services/oom_watcher.py`

- `ServiceType.RESOURCE_TUNING` + `ServiceDefinition` met `kind=ServiceKind.SYSTEM` uit taak 9. De
  coverage-guard (`tests/test_service_providers.py`) eist een registry-entry voor elke
  `ServiceType`, dus die regel hoort er meteen bij.
- `catalog/resource_tuning/__init__.py` overschrijft `observe_deployment`: leest de OOM-signalen uit
  de context, roept de tuning-logica aan met de betrokken componenten (taak 2), compacteert
  de history (taak 7) en geeft een `ObservationOutcome` terug.
- Vervang de `if oom_failures:`-tak in `project_manager.py:3061` door
  `services_for_hook(HookPoint.AFTER_SYNC)`.
- Doe hetzelfde voor `oom_watcher.py:592`. **Beide aanroepers moeten door dezelfde scan.**
  Eén ervan hardgecodeerd laten betekent dat we niets ontkoppeld hebben.

Twee dingen bewust buiten scope:

- **De `auto-tune-resources`-opt-out blijft staan waar hij staat.**
  `extract_auto_tune_enabled()` leest een bestaand YAML-veld. Dat naar een service-configblok
  verhuizen is een schemamigratie over alle projectbestanden, met eigen risico en geen
  functionele winst nu. Later samen te trekken, niet in dit plan.
- **Image-pull-afhandeling blijft voorlopig inline.** Die kan later hetzelfde hookpunt
  gebruiken, en dat is meteen het bewijs dat de abstractie zijn plek verdient. Maar één
  concern tegelijk verplaatsen.

Verificatie: de OOM-afhandeling verdwijnt volledig uit `project_manager.py` en
`oom_watcher.py`, en de sandbox-deploy uit taak 10 gedraagt zich identiek.

### 10. Gesloten waardenverzamelingen op services worden enums

`opi/services/services.py`, `opi/services/services_enums.py`, `opi/services/catalog/*/__init__.py`,
`opi/manager/delete_project_manager.py`

Als we voor het nieuwe hookpunt een enum eisen, moeten de bestaande velden met een vaste
waardenverzameling mee. Nu is een typefout daarin pas op runtime zichtbaar. Drie velden
komen in aanmerking, alle drie een kale `Enum` in `services_enums.py`:

| Nieuw | Waarden | Lezers vandaag |
|---|---|---|
| `ServiceScope` | `COMPONENT`, `DEPLOYMENT` | `services.py:699,705` (`== "component"`), `forms/visualizers/providers.py:121,130`, `templates/project-details/section-services.html.j2:22` |
| `CleanupStrategy` | `NONE`, `IMMEDIATE`, `DEFERRED` | `services.py:765` (`!= "none"`), plus zeven declaraties op regel 512-587 |
| `ManagerKey` | `DATABASE`, `MINIO`, `REDIS`, `KEYCLOAK`, `PVC` | `catalog/base.py:445`, zeven `cleanup_manager_key`-declaraties, en de resolver hieronder |

`ManagerKey` is de duidelijkste winst. `_get_manager_for_service`
(`delete_project_manager.py:2254`) is een vijfvoudige `if manager_key == "..."`-keten die
eindigt in `raise ValueError(f"Unknown manager key: {manager_key}")`. Een typefout in
`cleanup_manager_key` is daarmee een runtimefout tijdens het opruimen van een echte service.
Met een enum is het een pyright-fout, en kan de keten een dict-lookup worden.

**Niet omzetten**: `name`, `description`, `icon`, `color`, `help_template`, `backup_label`,
`config_section_id`, `modal_flow_id`. Dat zijn vrije tekst, ROOS-designtokens of losse
identifiers zonder gesloten verzameling. Een enum daarop is ceremonie zonder winst.

**Trap: templates renderen deze waarden.** Een kale `Enum` rendert in Jinja als
`ServiceScope.COMPONENT`, niet als `component`. `section-services.html.j2:22` doet
`{{ service_def.scope|title }}` en `providers.py:130` stopt de waarde in een dict die
richting de view gaat. Beide moeten `.value` gebruiken, anders verandert er zichtbaar iets
in de UI zonder dat een test omvalt.

Verificatie:

- `uv run pyright` is hier het echte vangnet: na de omzetting markeert die elke overgebleven
  vergelijking met een string-literal.
- `grep -rn '== "component"\|== "deployment"\|"immediate"\|"deferred"' opi/` levert niets meer op
  buiten de enumdefinitie.
- `uv run pytest tests/test_golden_manifests.py -q` blijft groen, want er mag niets aan de
  gerenderde output veranderen.
- De projectdetailpagina in de sandbox toont nog steeds "Component scope" en niet
  "Servicescope.Component".

### 11. Regressietest op het volledige pad

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

- **De projectpagina-geheugencheck blijft los.** Na taak 8 en 9 lopen de inline-detectie en de
  fire-and-forget watcher via hetzelfde hookpunt, maar de passieve check op de projectpagina
  niet. Die draait op een paginabezoek, niet op een deploy, en heeft dus een eigen aanleiding.
  De bredere samentrekking staat in `features/futures/system-wide-oom-watcher.md`.
- **Interactie met sleep-mode nagaan.** Een slapende deployment is niet `Available`, en taak 1
  haalt juist die guard weg voor het OOM-pad. Waarschijnlijk onschadelijk, want zonder pods is
  er ook geen OOM-signaal, maar dat is het verifiëren waard voordat taak 1 landt.
- **pr-450 zit nu vast.** De codefix staat niet in productie, dus deze deployment komt er niet
  vanzelf uit. Handmatig herstel: een `resources`-override op `deployments[pr-450].components[api]`
  met minstens 135Mi, of de root terug naar 75Mi. Dat is een aparte, bewuste actie.
- **Silent failure.** Dit past in het bekende patroon dat mislukte reprocessing en validatie
  alleen in het log landen. Alarmering hierop is een eigen traject.
- **Systeembrede watcher.** `features/futures/system-wide-oom-watcher.md` beschrijft OOM's die
  ná een geslaagde deploy ontstaan. Andere scope, geen overlap met dit plan.

## Veldgeval 2026-07-30: headscale op 25Mi, vier OOMKills in twee minuten

Toegevoegd vanuit het VLAM-gateway-traject (`vlam.md`). Dit is een gemeten productiegeval dat één
gebrek blootlegt dat nog niet in de taken hierboven staat.

### Wat er stond

De tuner schreef op 2026-07-28 om 23:07 op component `headscale` in `rig-prd-vlam-wt8`:

```
requests: {memory: 25Mi, cpu: 32m}
limits:   {memory: 25Mi, cpu: 500m}
```

met als reden: `Request: max 15Mi + 25% = 25Mi. Limit: max 15Mi x 1.5 = 25Mi`.

### Wat er misging

Twee dagen later werden twee nodes uit headscale verwijderd. Hun clients bleven pollen, wat een
stroom `node not found` opleverde. Dat was genoeg om over 25Mi te gaan. Gevolg: **OOMKilled,
viermaal binnen twee minuten**, en van buiten alleen een `HTTP 503` op de publieke Route. Voor
schaalgevoel: de twee buurcomponenten in dezelfde namespace gebruikten op dat moment 36Mi en 136Mi.

### Het gebrek dat hier bovenop komt

**De tuner zette `limit` gelijk aan `request`.** Dat is iets anders dan het vloer-probleem uit taak
4: ook mét een vloer blijft een component dat `limit == request` heeft bij de eerste piek dood. Een
Go-dienst alloceert op aanvraag, dus er hoort altijd ruimte tussen request en limit te zitten. De
huidige formule (`request = max × 1.25`, `limit = max × 1.5`) levert bij lage waarden na afronding
tweemaal hetzelfde getal op, en juist daar is de marge het hardst nodig.

**Het faalt bovendien versterkend in plaats van dempend.** OOMKill leidt tot herstartende clients,
die harder pollen, wat meer geheugen kost, wat de volgende OOMKill oplevert. Zonder ingrijpen komt
dat niet tot rust.

**En de meting was niet representatief.** De 15Mi was gemeten op een net gestarte headscale zonder
nodes en zonder relay-verkeer. Een component dat nog nooit onder realistische last heeft gestaan,
is geen goede basis om naar beneden te tunen.

### Taak: minimale marge tussen request en limit

`opi/services/resource_tuning_service.py`

Zorg dat een geschreven memory-`limit` altijd meetbaar boven de `request` uitkomt, ongeacht hoe
klein de meting was. Een absolute marge (bijvoorbeeld minstens 64Mi erboven) is hier robuuster dan
een factor, want een factor op een klein getal blijft klein.

Verificatie: unit-test met een gemeten maximum van 15Mi. De geschreven `limit` moet aantoonbaar
hoger zijn dan de `request`, en beide boven de vloer uit taak 4.

### Handmatig herstel dat is toegepast

`resources` op 128Mi/512Mi gezet, `auto-tune-resources: false` op dat component, en de
tuner-`history` verwijderd zodat er niet opnieuw naar 25Mi wordt teruggerekend. Dat laatste is
symptoombestrijding: zolang de marge niet is afgedwongen, blijft dit voor elk stil, klein component
mogelijk.
