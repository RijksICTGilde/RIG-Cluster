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

`--sync-timeout=600` in `spec.controller.extraCommandArgs` van de ArgoCD-CR, in alle drie de
overlays (`odcn-production`, `sandboxed-local`, `local`). De controller zet dan
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
aanroepplekken, met een andere drempel, en dat verschil is het punt:

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
  gewoon bezig zijn, dus de drempel hoort er wel.

Op beide plekken blijft `blocks_next_sync` de grendel: hangt `.operation` er nog aan en is de
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

Open punt: welk endpoint `zadctl deployment refresh` aanroept is hier niet te zien, want
zadctl staat in een andere repo. Het plan van deze taak stelt dat het alleen ArgoCD's
vergelijking opnieuw aanzet (`GET /api/v1/applications/{app}?refresh=normal`); OPI zelf
heeft geen endpoint met die vorm. Klopt die stelling, dan moet zadctl naar de OPI-endpoints
hierboven wijzen voordat deze uitweg bij de gebruiker aankomt.

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
