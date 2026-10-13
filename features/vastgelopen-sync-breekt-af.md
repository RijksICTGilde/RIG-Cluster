# Een sync die vastloopt breekt af

Een ArgoCD-sync die op de gezondheid van een wave wacht eindigt nooit als die wave niet
gezond wordt, en zolang hij loopt wordt elke volgende sync op die Application overgeslagen.
Een gebruiker kan zijn kapotte spec dan niet meer repareren, precies op het moment dat hij
dat nodig heeft. Er zijn nu drie grendels, en ze werken op verschillende niveaus.

De aanleiding en alle metingen staan in
`plans/een-sync-die-niet-afbreekt-blokkeert-elke-volgende.md`.

## Wanneer dit speelt

Alleen als er in dezelfde sync iets te PRUNEN valt. `PruneLast=true` zet prune-taken op een
eigen wave ná alle andere, en een wave gaat pas lopen als de vorige gezond is. Zonder prune
is wave 0 de laatste wave, en dan rondt de sync af ongeacht of de pods ooit ready worden.

Er valt iets te prunen bij: de waker van sleep-mode (komt bij een sweep, gaat bij een wake),
een component dat uit een deployment gaat, een service die uitgezet wordt, storage die
verdwijnt, en OPI's eigen opruimronde van verouderde component-manifests.

`PruneLast=true` blijft staan, en dat is gemeten en geen aanname: zonder die optie verdwijnt
bij een PVC-generatiewissel de vorige generatie meteen, ook als de nieuwe pod nooit start,
en dan is de data weg terwijl ArgoCD `Synced` meldt.

## De drie grendels

### 1. De controller breekt na tien minuten af

`--sync-timeout=600` in `spec.controller.extraCommandArgs` van de ArgoCD-CR, in beide
overlays (`odcn-production` en `sandboxed-local`). De controller zet dan
`phase: Terminating`, laat de sync afbreken met `Operation terminated`, en start in dezelfde
seconde een nieuwe sync op de NIEUWSTE revisie. Een fix die al in git stond rolt daarmee
alsnog uit, zonder dat iemand iets doet.

600 en niet 300: ruim boven OPI's eigen wacht van 300s, zodat OPI's rapport eerst komt en de
controller het vangnet blijft in plaats van de eerste die afkeurt.

Dit is een instelling op de ArgoCD-INSTANTIE en geldt dus voor elke Application op dat
cluster. Een grens per Application bestaat niet in ArgoCD
([#15932](https://github.com/argoproj/argo-cd/issues/15932) staat open sinds 2023).

**Als flag, niet als `extraConfig`-key.** De operator kopieert `spec.extraConfig` in
`argocd-cm`, en `controller.sync.timeout.seconds` is een cmd-params-key die daar niemand
leest. En `spec.cmdParams` helpt ook niet: deze operator bouwt zijn eigen StatefulSet en
wiret daar op één key na geen enkele cmd-params-key aan een component.
`tests/test_argocd_extra_config_keys.py` houdt dat vast.

### 2. OPI laat nooit een vastgelopen operation achter

`opi/services/argocd_operation.py` leest `.operation` en `status.operationState`, en
`terminate_stuck_operation` beëindigt een operation die een volgende sync blokkeert. Twee
aanroepplekken en drie drempelkeuzes met twee waarden, want de ronde vlak voor de push kiest
zijn drempel op grond van zijn aanroeper. Dat verschil is het punt:

* de `except TimeoutError`-tak van `_refresh_and_wait`, via
  `_terminate_operation_we_waited_on`, met `threshold_seconds=0`: de operation waarop OPI
  stond te wachten laten staan maakt van een mislukte uitrol ook een blokkade op elke
  volgende. Een drempel hoort hier niet. De operation is per constructie JONGER dan OPI's
  eigen wacht, want `wait_for_application_synced` start zijn teller pas na de refresh en de
  render, en ArgoCD begint de operation daarna. Met de standaarddrempel bleef precies de
  blokkade staan die deze tak moet opruimen (gemeten: 299s oud gaf nul patch-aanroepen). En
  "nog gewoon bezig" is hier geen mogelijkheid meer: OPI heeft net 300s gewacht en opgegeven;
* vlak VOOR `push_changes()` in `_process_repository_manifests`, via
  `_clear_blocking_sync_operations`, met `STUCK_OPERATION_THRESHOLD_SECONDS` (300s, gelijk
  aan OPI's eigen wacht): wat we pushen moet uitgerold kunnen worden, ook als een andere taak
  de blokkade achterliet. Hier kijkt OPI naar een uitrol van iemand anders, en die kan nog
  gewoon bezig zijn, dus de drempel hoort er wel. Deze ronde loopt ook bij de push van de
  sleep-mode-sweeper, van de resource-tuner (`resource_tuning_service.py`) en van de refresh
  die de OOM-watcher voor zichzelf in de wachtrij zet;
* diezelfde ronde op een REFRESH, met drempel 0: daar drukt iemand op reset. Zie hieronder.

Op alle drie blijft `blocks_next_sync` de grendel: hangt `.operation` er nog aan en is de
phase `Running` of `Terminating`. Een losgekoppelde stand blokkeert niets en blijft staan. Een
leeftijd die door klokverschil negatief uitvalt (ArgoCD logt in UTC, OPI's eigen regels staan
in CEST) leest als 0, anders zou een drempel van 0 juist de blokkade laten staan die hij moet
opruimen.

**Een opruiming die niet lukt gijzelt de levering niet.** `terminate_stuck_operation` vangt
`KubectlConnectionError` en `KubectlExecutionError` zelf af en meldt dan "niet beëindigd": de
ronde voor de push staat TUSSEN de commits en `push_changes()`, dus stranden zou de manifests
gecommit en ongepusht achterlaten, en op de time-out-tak zou het van een nette time-out een
kubectl-fout maken. Die tak is niet zeldzaam: `_run_kubectl_command` gooit zodra
`KubectlConnector.isConnected` False is, en die vlag gaat procesbreed om bij één "connection
refused", met een achtergrondretry van 30s. De blokkade blijft dan staan tot de volgende ronde,
en dat is beter dan een halve levering.

Die volgorde is niet vrijblijvend. ArgoCD probeert een revisie die al eens faalde niet
opnieuw (`Skipping auto-sync: failed previous sync attempt`). Beëindigen vóór de push
betekent dat de revisie erna nieuw is, en dan loopt auto-sync wel.

**Dit is tegelijk de uitweg voor de gebruiker.** De refresh-endpoints van OPI
(`POST /api/v2/projects/{p}/deployments/{d}/:refresh` en de v1-variant) draaien de
`refresh_deployment`-taak, die via `process_project` langs dit pad komt. Eén refresh is dus
genoeg; de deployment weggooien hoeft niet, en er is met opzet geen tweede knop bijgekomen.

`zadctl deployment refresh` komt daar ook uit: de cli doet
`POST /v2/projects/{p}/deployments/{d}/:refresh`, gemeten in de repo `zad-cli`
(`tests/test_spec.py` en `tests/test_rollout_config.py` noemen dat pad, in deze repo niet na
te meten).

#### Een refresh is een resetknop (drempel 0)

Een reset die soms wel en soms niet reset is erger dan een uitrol van enkele seconden die
opnieuw begint: dat kost seconden en breekt niets. De vlag `reset_running_sync` zet de
drempel daarom op 0, precies zoals de time-out-tak dat doet.

**De vlag hangt aan het endpoint, niet aan de handler.** `handle_refresh_project` wordt ook
intern aangeroepen, uit het component-delete-pad, en dat zou het gedrag anders stilzwijgend
erven. Wie hem zet:

| plek | zet hem | waarom |
|---|---|---|
| `POST /api/v2/projects/{p}/:refresh` | ja | de resetknop op projectschaal |
| `POST /api/v2/projects/{p}/deployments/{d}/:refresh` | ja | dezelfde knop op een deployment, en wat zadctl aanroept |
| `POST /projects/{p}/refresh` en `/projects/{p}/refresh/{d}` | ja | de knop "Herverwerken" op de projectpagina post hierheen en niet naar v2 (`opi/web/project_actions.py` bouwt dat pad), dus zonder dit reset de resetknop niets |
| `GET /api/projects/{p}/:refresh` en de deployment-variant (v1) | ja | hetzelfde gebaar op de oudere API; de projectvariant draait zelfs nog inline |
| het component-delete-pad | ja, expliciet | een verwijdering moet landen, en een sync op de oude staat hoort dat niet te vertragen |
| sleep-mode-sweeper, resource-tuner, OOM-watcher, en elke andere push | nee | die houden hun drempel van 300s |

De regel is dus: elk endpoint dat een refresh IS zet hem, en niets anders. Daarom zijn de
v1-endpoints en de web-knoppen meegenomen en niet alleen de twee v2-endpoints.

Op PROJECTniveau geldt het voor elke deployment in het project, niet alleen voor de deployment
die vastzit. Dat is bij een project met twintig deployments een grote hamer, en dat is de
bedoeling: de knop zit op het project.

**En het wordt hardop gezegd.** Is er werkelijk een operation weggehaald, dan staat dat als
`processing.terminated_syncs` in de uitkomst: in de taakuitkomst, en op de twee inline
v1-takken (het projectendpoint, en het deploymentendpoint met `sync=true`) in het antwoord
zelf. Loopt het als taak, dan komt er ook een voortgangsstap bij ("Lopende sync afgebroken:
\<deployment\>"); inline is er geen taak en dus geen voortgang. Zonder dit ziet de
ontwikkelaar van wie de uitrol werd afgebroken een mislukking zonder oorzaak. Liep er niets,
dan staat er niets. Dat geldt voor beide drempels, dus ook een gewone push meldt het als hij
een vastgelopen sync afbrak.

### 3. Het rapport vertelt wat er werkelijk loopt

`GET /api/v2/projects/{p}/deployments/{d}` draagt:

| veld | wat het zegt |
|---|---|
| `running_sync.phase` | `Running` of `Terminating`, of het veld is `null` |
| `running_sync.started_at` | sinds wanneer |
| `running_sync.revision` | de revisie waarop deze sync vastzit; kan afwijken van `sync_revision` |
| `running_sync.blocks_next_sync` | of hij elke volgende sync tegenhoudt |
| `rolled_out_revision` | wat de laatste sync werkelijk uitrolde (`syncResult.revision`) |
| `last_synced_at` | wanneer de laatste sync AFRONDDE, en `null` zolang er een loopt |

`last_synced_at` viel eerder terug op `status.reconciledAt`, de tijd van de laatste
VERGELIJKING. Dat gaf de indruk dat een wijziging was aangekomen terwijl er niets was
uitgerold. Dezelfde terugval stond ook op het dashboard, waar hij `project.last_deployed`
voedde en als "3 uur geleden" op het scherm kwam; die is mee weggehaald. Geen afgeronde sync
betekent daar nu geen regel, en dat is beter dan een datum die niet van een uitrol is.

Op de projectpagina heet dit "Sync van ArgoCD", met de duur erbij en, zodra hij vastligt (hij
blokkeert en loopt al 300s of langer, of hij heeft geen leesbaar begin), een regel die zegt
dat een nieuwe versie niet op het cluster komt. Niet "Operatie": ZAD's eigen wachtrij
(`pending_rollout.rollout_in_progress`) stond onder datzelfde woord op het scherm, en die twee
kunnen los van elkaar aanstaan.

## Wat er niet mee opgelost is

* **Een halve apply blijft liggen.** Een afgebroken sync laat de prune ongedaan, dus de
  Application staat `OutOfSync` met resources die niet meer in git staan. Dat lost zichzelf
  op zodra de oorzaak weg is (gemeten: `Synced` twee seconden na de reparerende commit, de
  restanten zelf weg na negen), en eerder KAN het niet.
* **Een operation waar niemand naar kijkt.** Alarmeren staat als eigen taak in
  `features/futures/alarmeren-op-een-sync-of-verwijdering-die-blijft-staan.md`.

## Verwante documenten

* `features/argocd-vastgelopen-verwijdering.md` - dezelfde blokkade, maar bij een
  verwijdering.
* `features/config-hash-pod-restart.md` - waarom een secretwijziging het pod-template
  verandert en dus een uitrol vraagt.
