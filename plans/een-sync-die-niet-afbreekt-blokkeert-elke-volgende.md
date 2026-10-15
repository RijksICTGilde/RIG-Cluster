# Een sync die niet afbreekt blokkeert elke volgende

Op 5 oktober 2026 kon `dd-mco/pr-68` 45 minuten lang geen nieuwe pod-spec meer krijgen. De sync van 14:54:30 wachtte op een Deployment die nooit gezond werd, bleef daardoor `Running`, en elke volgende sync werd overgeslagen met `Skipping auto-sync: another operation is in progress`. De klant heeft de deployment weggegooid en opnieuw aangemaakt, want dat is de enige actie die die geblokkeerde sync niet nodig heeft.

Dit plan gaat niet over die ene deployment. Het gaat over de eis eronder: een gebruiker moet een kapotte spec altijd kunnen repareren, ook terwijl de huidige pod niet ready is. Dat is precies het moment waarop je het nodig hebt. ArgoCD mag een uitrol begrenzen en mag hem afkeuren, maar mag nooit onbepaald lang de enige weg naar het cluster dichthouden.

Alle tijden in dit document staan in UTC. Let op bij het nameten: de ArgoCD-controller logt in UTC, OPI's eigen regels staan in CEST.

## Wat er gebeurde

```
14:54:11  OPI commit 585aa8c31 in de projects-repo (update pr-68)
14:54:19  OPI: "Pruning 3 obsolete component manifest(s) from pr-68" (de waker)
14:54:20  6a0295a671 in de deployments-repo
14:54:30  ArgoCD: "Initiated automated sync to '6a0295a6...'", phase Running
14:54:36  operation: "waiting for healthy state of apps/Deployment/pr-68-website"
14:54:45  readiness probe 503 (GH_RELEASE wees naar een verwijderde release)
15:13:24  taak 3ca50db1 pusht niets nieuws, wacht 300s, geeft op, laat de operation staan
15:30:34  d65d2e8eaf: de GH_RELEASE-fix, alleen website-user-secret.sops.yaml
15:40:08  deployment verwijderd
15:42:08  opnieuw aangemaakt, sync naar 33cf3b50 Succeeded in 3 seconden
```

Tussen 14:54:34 en 15:39 staat er in de controller-log voor `dd-mco-pr-68` uitsluitend `Skipping auto-sync: another operation is in progress`. Geen enkele andere reden.

De tasklijst van die vastgelopen operation is om 14:54:33 en om 15:38:33 identiek:

```
Sync/-1  Secret pr-68-website-platform, pr-68-website-user      obj->obj  Succeeded
Sync/0   NetworkPolicy, Service, Ingress, VPA                   obj->obj  Succeeded
Sync/0   Deployment pr-68-website                               obj->obj  "configured"
Sync/1   Secret pr-68-website-waker-token                       obj->nil
Sync/1   ConfigMap pr-68-website-waker-config                   obj->nil
Sync/1   Deployment pr-68-website-waker                         obj->nil
```

Daarin staat het hele probleem. De `obj->nil` zijn prune-taken, en ze staan op een eigen wave ná 0 omdat `PruneLast=true` dat zo doet. Een wave gaat pas lopen als de vorige gezond is. Wave 0 bevatte de Deployment die 503 gaf, dus wave 1 ging nooit lopen, dus de operation eindigde nooit. En de inhoud van die lijst verandert niet meer: de operation zit vast aan revisie 6a0295a6 en ziet nieuwere commits niet. Er waren twee revisies tegelijk in het spel, de vergelijking op d65d2e8e en de operation op 6a0295a6, en de operation houdt de lock.

De pending prune houdt zichzelf in stand. Hij verdwijnt alleen als een sync hem uitvoert, en hij is zelf de reden dat geen sync afrondt. Daarom hielp `zadctl deployment refresh` niet: dat is `GET /api/v1/applications/{app}?refresh=normal`, dat hertoetst alleen de vergelijking, start geen sync en beëindigt geen operation. Een nieuwe image had ook niet geholpen: die verandert het manifest in git, en dat toepassen vraagt dezelfde geblokkeerde sync.

### Wat wél werkte

De config-hash deed zijn werk. De CMP berekent een SHA256 over alle Secrets en ConfigMaps in de gerenderde output en zet die als `checksum/config` op het pod-template (`bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml:383-402`, beschreven in `features/config-hash-pod-restart.md`). De GH_RELEASE-fix raakte alleen het user Secret, maar daarmee verandert die hash, en dus het pod-template. Dat is ook precies wat `describe` meldde: `Deployment/pr-68-website wijkt af van git`. Die afwijking was de nieuwe pod-spec die stond te wachten. Er is op dat vlak niets te repareren.

### Waarom dit normaal niet gebeurt

ArgoCD wacht alleen op de health van een wave als er daarna nog werk is. De laatste wave wordt toegepast en dan is de operation klaar, ongeacht of de pods ooit ready worden.

In een ZAD-deployment-Application bestaan maar twee waves: secrets op `-1`, al het andere op `0`. Geen enkele template onder `manifests/` zet een wave op een Deployment, Service, Ingress, ConfigMap of VPA, en er staan nergens `argoproj.io/hook`-annotaties. De enige resources met wave `1` in de hele `odcn-production`-boom zijn twee db-clusters, en die liggen in `infrastructure/`-mappen, dus in een andere Application.

Wave 0 is dus normaal de laatste wave, en een pod die niet start is dan niets meer dan een rood bolletje. `PruneLast=true` maakt uit het niets een wave ná 0, zodra er iets te prunen valt, en daarmee wordt de health van wave 0 een gate.

Diezelfde storing bevat de A/B-meting, twee keer dezelfde applicatie met 48 minuten ertussen:

```
14:54:30  mét prune-taken      -> "waiting for healthy state of apps/Deployment/..."  45 min Running
15:42:37  zonder prune-taken   -> "Succeeded (all tasks run)"                          3 seconden
```

De tweede sync heeft de health van de Deployment niet eens bekeken.

### Wanneer valt er iets te prunen

Een gewone uitrol regenereert dezelfde set manifests, dus dan is er niets te prunen en bestaat het gevaar niet. Er verdwijnt pas iets bij: de waker van sleep-mode (komt bij een sweep, gaat bij een wake, drie resources per keer), een component dat uit een deployment gaat, een service die uitgezet wordt, storage die verdwijnt, en OPI's eigen opruimronde van verouderde component-manifests.

Dat verklaart waarom het een `pr-*` was. Daar staat sleep-mode aan met een waker, dus elke wake levert drie prunes op, en een PR-preview is ook het soort deployment dat regelmatig stuk is. De twee helften van de voorwaarde komen daar veel vaker samen dan ergens anders.

## Wat we daarnaast vonden

De bovengrens die dit had moeten afbreken staat al in de repo, op een plek waar de controller hem niet leest.

`bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml:23` zet `controller.sync.timeout.seconds: "300"` binnen `extraConfig`. De operator-CRD beschrijft `extraConfig` als "add fields to Argo CD configmap", dus `argocd-cm`. Maar dat is een cmd-params-key: hij hoort in `argocd-cmd-params-cm` en wordt daar de `--sync-timeout`-flag van de application-controller.

Twee metingen uit prod, los van wie de operator installeert:

* de live `argocd-cmd-params-cm` in `rig-prd-operations` bevatte twee keys, `applicationsetcontroller.enable.tokenref.strict.mode` en `controller.resource.health.persist`, en geen van beide staat in onze repo. Onze keys stonden er niet in.
* in 2,5 uur controller-log staat geen enkele regel `Terminating in-progress operation due to timeout`, terwijl die sync negen keer over de 300 seconden ging.

De instelling staat er sinds 12-01-2026 en is al die tijd dood geweest. Het is niet die ene regel: in hetzelfde blok staan ook `controller.status.processors`, `controller.operation.processors`, `controller.repo.server.timeout.seconds`, `controller.kubectl.parallelism.limit`, `controller.sharding.algorithm`, `reposerver.parallelism.limit` en `server.repo.server.timeout.seconds`, en dat zijn allemaal cmd-params-keys. Alleen `timeout.reconciliation` en `timeout.hard.reconciliation` horen echt in `argocd-cm` en werken.

Nagelezen in de operator-broncode, zodat dit niet op de CRD-tekst alleen leunt. `controllers/argocd/configmap.go:519` kopieert `cr.Spec.ExtraConfig` in de ConfigMap die `reconcileArgoConfigMap` bouwt, en dat is `common.ArgoCDConfigMapName`, dus `argocd-cm`. De operator-documentatie zegt hetzelfde: "Users can add dynamic entries to Argo CD configmap using `ExtraConfig`".

### De valkuil bij het repareren

De voor de hand liggende verhuizing is naar `spec.cmdParams`, want dat veld vult `argocd-cmd-params-cm` (`configmap.go:1022`). Dat werkt hier niet, en dat is precies waarom deze alinea er staat.

In een gewone ArgoCD-installatie wiret het StatefulSet-manifest elke cmd-params-key aan een env-variabele, bijvoorbeeld `ARGOCD_APPLICATION_CONTROLLER_SYNC_TIMEOUT` met `configMapKeyRef` naar `controller.sync.timeout.seconds` en `optional: true`. De operator bouwt zijn eigen StatefulSet en doet dat niet: hij zet precies twee van die env-variabelen (`ARGOCD_APPLICATION_CONTROLLER_PERSIST_RESOURCE_HEALTH` en `ARGOCD_RECONCILIATION_TIMEOUT`) en geeft de rest als getypeerde flags mee, uit `spec.controller.*`. In de hele operator-broncode komt `sync-timeout` of `sync.timeout` niet voor. Een key in `argocd-cmd-params-cm` bereikt de controller dus ook niet.

Wat overblijft zijn `spec.controller.extraCommandArgs` en `spec.controller.env`.

### Waarom dit nooit is opgevallen

Nagemeten op odcn-production op 5 oktober 2026. `argocd-cm` bevat onze tien keys, inclusief `controller.sync.timeout.seconds = '300'`. Het command van `argocd-application-controller-0` is:

```
argocd-application-controller --operation-processors 10 --redis ... --repo-server ...
  --status-processors 20 --kubectl-parallelism-limit 10 --loglevel info --logformat text
  --persist-resource-health --repo-server-timeout-seconds=180
```

Geen `--sync-timeout`. En bij de env-variabelen staan precies twee `configMapKeyRef`-verwijzingen, `ARGOCD_APPLICATION_CONTROLLER_PERSIST_RESOURCE_HEALTH` naar `argocd-cmd-params-cm` en `ARGOCD_RECONCILIATION_TIMEOUT` naar `argocd-cm`, net zoals de operator-broncode voorspelt.

Daarmee is per key te zeggen wat er werkelijk gebeurt, en waarom niemand iets zag:

| key in `extraConfig` | onze waarde | wat er draait | hoe |
|---|---|---|---|
| `timeout.reconciliation` | 15m | 15m | werkt, dit is een echte `argocd-cm`-key |
| `timeout.hard.reconciliation` | 0s | 0s | werkt, idem |
| `controller.repo.server.timeout.seconds` | 180 | **180** | niet via deze key, maar via `spec.controller.extraCommandArgs` en `.env`, die hetzelfde al zetten |
| `server.repo.server.timeout.seconds` | 180 | **180** | idem, via `spec.server.extraCommandArgs` |
| `controller.status.processors` | 20 | 20 | niet via deze key; de operator-default is 20 |
| `controller.operation.processors` | 10 | 10 | idem, operator-default 10 |
| `controller.kubectl.parallelism.limit` | 10 | 10 | idem, operator-default 10 |
| `controller.sharding.algorithm` | round-robin | niet gezet | sharding staat bewust uit in dezelfde CR |
| `reposerver.parallelism.limit` | 10 | 0, onbeperkt | dood, en alleen onder belasting zichtbaar |
| `controller.sync.timeout.seconds` | 300 | **0, dus nooit** | dood, en dit is de enige die het gedrag verandert |

Zeven van de tien keys zijn dood, en van die zeven veranderen er zes niets: drie zijn gelijk aan de operator-default, één hoort bij een functie die bewust uit staat, en twee staan al langs de werkende route in dezelfde CR. Dat laatste is het verraderlijke: iemand heeft die twee timeouts eerder al naar `extraCommandArgs` en `env` verplaatst, en de kopie in `extraConfig` laten staan. Daardoor leek het blok te werken, want de dingen die je zou nameten werkten inderdaad.

Blijft over de enige die echt dood is én iets doet. En die is wél opgevallen: twee keer, als deze storing van 45 minuten en als de verwijdering in `rig-prd-mpfm-w3h` die veertien dagen bleef hangen. Beide keren is het symptoom aan iets anders toegeschreven. Niemand heeft de instelling gemist, we hebben de gevolgen ervan niet aan die instelling gekoppeld.

Daarmee is de conclusie in `features/argocd-vastgelopen-verwijdering.md` ("die grens breekt een hangende sync niet af") niet gemeten maar beredeneerd: de sandbox-overlay zet de key nergens, en op odcn-production kwam hij nooit bij de controller aan.

## Wat upstream hierover zegt

Nagelezen in de broncode van de versie die wij draaien, tag v3.5.1.

* De health-gate vóór de prune-wave is bedoeld gedrag. Het staat letterlijk in het geaccepteerde feature request dat `PruneLast` heeft gebouwd, [#5080](https://github.com/argoproj/argo-cd/issues/5080): "after the other resources have been deployed **and become healthy**".
* Auto-sync overslaan zolang er een operation loopt is bedoeld: `autoSync` begint met `if app.Operation != nil` (`controller/appcontroller.go:2336`).
* Een bovengrens bestaat en werkt echt: `controller/appcontroller.go:1542-1547` zet bij overschrijding `phase: OperationTerminating` met bericht `operation is terminating due to timeout` en laat de sync afbreken, en regel 1586-1588 plant die controle in bij het starten van elke operation. De flag heet `--sync-timeout`, met env `ARGOCD_APPLICATION_CONTROLLER_SYNC_TIMEOUT`, default 0 betekent nooit.
* Een timeout **per Application** bestaat niet: [#15932 "Implement Sync timeout"](https://github.com/argoproj/argo-cd/issues/15932) staat open sinds 2023. Dat is wat de verwijderde `Timeout=300`-syncOption suggereerde te zijn.
* Het dichtstbijzijnde open issue met ons symptoom is [#29636](https://github.com/argoproj/argo-cd/issues/29636) van 09-09-2026: auto-sync eeuwig overgeslagen, handmatige sync geweigerd. Andere oorzaak (een `operationState` met `phase: Succeeded` en `syncResult: nil` na een herstart van de controller) en daar helpt de sync-timeout inderdaad niet, omdat `isOperationInProgress` alleen op `!Phase.Completed()` kijkt. Een maintainer zegt dat [PR #28778](https://github.com/argoproj/argo-cd/pull/28778) dat verholpen heeft. Bij ons stond de phase op `Running`, dus dat is onze zaak niet.

Conclusie: dit is geen ArgoCD-bug waar we op een fix kunnen wachten. Het is onze configuratie plus een syncOption die wij zelf hebben gezet.

## Wat we willen

Eén regel, en daar is de rest aan te toetsen: **een nieuwe pod-spec moet altijd toegepast kunnen worden, ook als de huidige pod niet ready is.**

Daaruit volgen drie eigenschappen:

1. Geen enkele sync loopt onbepaald lang. Een uitrol die te lang duurt wordt afgebroken en gerapporteerd, niet stil volgehouden.
2. Een vastgelopen sync is nooit een eenrichtingsstraat. OPI kan hem altijd beëindigen, en de gebruiker kan dat via `zadctl` bereiken zonder zijn deployment weg te gooien.
3. Wat ZAD rapporteert klopt. Een lopende sync is te zien, met sinds wanneer, en `last_synced_at` liegt niet.

Zelf kubectl gaan doen lost dit op en kost te veel: we verliezen driftdetectie, selfHeal, de config-hash uit de CMP en het spoor in git. Het punt is niet dat GitOps hier verkeerd is, het punt is dat de apply van een gebruikersdeployment niet afhankelijk mag zijn van de gezondheid van de vorige versie.

## Wat deze taak moet opleveren

Naast de code uit "De stappen" hoort hier een antwoord bij op elke open vraag. Een vraag die open blijft is niet af, en een vermoeden is geen antwoord. Concreet:

1. **Elke open vraag krijgt een gemeten antwoord**, in dit document bijgewerkt, met het bewijs eronder: het commando dat je draaide en wat eruit kwam. Dit document staat nog niet in de repo: zet het als `plans/een-sync-die-niet-afbreekt-blokkeert-elke-volgende.md` op de branch van deze taak en werk het daar bij, zodat de antwoorden bij het plan blijven staan. Voor de vragen die een ontwerpkeuze zijn en geen meting (V7, V8) hoort er een voorstel met een afweging te staan, niet alleen de keuze.
2. **Kun je iets niet meten, schrijf dan op waarom niet** en laat de vraag open staan met die reden. Dat is een geldige uitkomst. "Ik zie het niet" is dat niet: afwezigheid van een symptoom is geen bewijs dat het er niet is, dus zeg wat je hebt gezocht en waar.
3. **De proeven draaien op de sandbox.** Dat cluster is gedeeld, dus claim het met `orch sandbox claim` en geef het daarna terug met `orch sandbox release`. Kun je er niet bij, meld dat en park de proeven; verzin geen uitkomst.
4. **Niets naar odcn-production in deze taak.** De wijziging aan de odcn-overlay mag voorbereid en in de PR gezet worden, maar het uitrollen is een apart besluit nadat de sandbox heeft laten zien wat het doet.
5. **Blijkt een aanname in dit document fout, corrigeer het document.** Het is geschreven op metingen van 5 oktober 2026 en op broncode van ArgoCD v3.5.1 en argocd-operator; als de sandbox iets anders laat zien, is de sandbox de waarheid en het document de fout.

## De stappen

Alles eerst op de sandbox, en pas naar odcn-production als de proef daar heeft laten zien wat hij hoort te doen. Dat geldt ook voor de instellingen die op het eerste gezicht een regel in een CR zijn: ze raken de hele ArgoCD-instantie en dus elke Application op dat cluster. `orch ship` kan de proeven in het sandboxcluster draaien.

### 1. De sync-timeout als flag, en de rest van het blok nalopen

**Waar**: `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml` en de overlays `sandboxed-local` en `local`.

Eén regel erbij, naast een regel die al bewijst dat de route werkt. De CR heeft op odcn-production al een `spec.controller.extraCommandArgs` met `--repo-server-timeout-seconds=180`, en die flag staat ook werkelijk in het command van de draaiende controller. Daar komt `--sync-timeout=600` naast.

Niet naar `spec.cmdParams`: onder deze operator bereikt die key de controller daar ook niet, zie de valkuil hierboven. En haal de dode keys uit `extraConfig` weg, zodat de CR niet langer iets belooft wat hij niet doet. Let op `controller.repo.server.timeout.seconds` en `server.repo.server.timeout.seconds`: die zijn in `extraConfig` dood maar staan langs de werkende route al goed, dus weghalen verandert daar niets. De sandbox-overlay heeft wel een `spec.controller.env` maar nog geen `extraCommandArgs`; die moet hij voor de proef dus krijgen.

600 seconden, want tien minuten is de bovengrens die een uitrol bij ons mag duren. Dat is ook ruim boven OPI's eigen 300s wacht, zodat OPI's rapport altijd eerst komt en de controller het vangnet is en niet de eerste die iets afkeurt.

Loop in dezelfde beweging het hele `extraConfig`-blok langs: `processors`, `parallelismLimit` en `sharding` hebben getypeerde velden onder `spec.controller`, en `timeout.reconciliation` hoort te blijven staan waar hij staat. Drie van die waarden zijn gelijk aan de operator-default, dus daar valt niets te winnen behalve dat de CR niet langer iets belooft wat hij niet doet.

**Verify**: de flag staat in het command van de draaiende controller. Lees dat van de pod en niet van de StatefulSet, want daar geeft onze RBAC op odcn-production `Forbidden`:

```
kubectl -n rig-prd-operations get pod argocd-application-controller-0 \
  -o jsonpath='{.spec.containers[0].command}'
```

Daarna in de sandbox: een sync die op een ongezonde pod wacht moet eindigen met `operation is terminating due to timeout`, en de eerstvolgende commit moet daarna gewoon uitrollen. Pas dit aan op odcn-production nadat de sandbox het heeft laten zien.

### 2. OPI laat nooit een vastgelopen operation achter

**Waar**: `operations-manager/python/opi/manager/project_manager.py`, de `except TimeoutError:`-tak van `_refresh_and_wait` (rond regel 3583), en het pad dat vlak voor een push de Application aanraakt.

Twee momenten, één bestaande helper. `KubectlConnector.terminate_argocd_application_operation` (`opi/connectors/kubectl.py:682`) verwijdert `/operation` met een json-patch en vangt "missing path" en "notfound" al netjes af. Hij wordt nu alleen door het delete-pad gebruikt (`delete_project_manager.py:115`), voor exact deze deadlock bij een verwijdering (#184).

* Geeft OPI's eigen wacht na 300s op, dan beëindigt hij de operation waar hij op stond te wachten. Nu laat hij hem staan, en dat is wat op 5 oktober om 15:18 een wedge van nog 21 minuten opleverde.
* Treft OPI vóór een push een `Running` operation aan die ouder is dan een drempel, dan beëindigt hij die eerst. Dat dekt een wedge die een andere taak of een sweep heeft achtergelaten.

Let op een gevolg dat uit de broncode blijkt: `processRequestedAppOperation` loopt alleen als `app.Operation != nil`, en er is geen pad dat een losgekoppelde `operationState` opruimt. Haal je `/operation` weg terwijl de phase `Running` is, dan blijft die `operationState` op `Running` staan tot een volgende sync hem overschrijft. Auto-sync is dan wel vrij, want die kijkt alleen naar `app.Operation`. Stap 3 moet die stand dus als verouderd kunnen lezen, en V4 vraagt of OPI ook `status.operationState` moet wissen.

**Verify**: een test die een `Running` operation ouder dan de drempel opzet en de terminate verwacht, een tweede die aantoont dat een jonge operation ongemoeid blijft, en een sandbox-run waarin na de terminate de nieuwste revisie daadwerkelijk uitrolt.

### 3. Rapporteer de echte sync-stand

**Waar**: `opi/api/v2/router.py:261` en `opi/services/deployment_diagnostics.py:266`.

`last_synced_at` is nu `operation_state.get("finishedAt") or status.get("reconciledAt")`. Bij een lopende operation is er geen `finishedAt`, dus rapporteert hij de tijd van de laatste refresh. Dat is wat op 5 oktober om 15:30:44 de indruk gaf dat de wijziging was aangekomen. Laat die terugval vallen, en rapporteer in plaats daarvan de operation zelf: phase, `startedAt`, en de werkelijk uitgerolde `syncResult.revision`. Dat laatste is wat je wil zien, want `sync_revision` is `status.sync.revision` en dat is de revisie waartegen vergeleken is.

`gather_sync_deviations` leest `operationState` al en kent het onderscheid "hier verandert geen sync meer iets aan" voor het geval van een vastgelopen finalizer. Breid dat uit: loopt er een operation sinds lang, dan is de regel niet "wordt bij de volgende sync bijgewerkt" maar "er loopt sinds HH:MM een sync die hierop vastzit". En `rollout_in_progress` gaat over ZAD's eigen wachtrij, niet over ArgoCD; dat moet uit elkaar te houden zijn op het scherm.

**Verify**: tests met een `operationState` op `phase: Running` met een oude `startedAt`, en met een losgekoppelde stand (phase Running zonder `app.Operation`).

### 4. Een uitweg voor de gebruiker

**Waar**: de v2-API en `zadctl`.

Vandaag is "deployment weggooien" de enige actie die een klant heeft, en die is niet veilig: volgens #184 kan de delete op diezelfde PruneLast-gate hangen, en dat is wat in `rig-prd-mpfm-w3h` veertien dagen lang 350 resources heeft laten liggen.

Voorstel, naam nog te kiezen: laat `deployment refresh` zelf de vastgelopen operation beëindigen wanneer hij er een aantreft, in plaats van er een apart commando naast te zetten. Dat is wat een gebruiker al probeerde, het is wat hij bedoelde, en het voorkomt een tweede knop die je moet kennen.

**Verify**: een e2e-test in de sandbox die de wedge maakt, `refresh` doet, en vaststelt dat er een nieuwe pod met de nieuwe waarde komt.

### 5. Alarmeren op een operation die blijft staan

Een operation die langer dan een kwartier `Running` staat, en een Application met een `deletionTimestamp` ouder dan een kwartier. Zonder dit was deze storing 45 minuten onzichtbaar en de mpfm-w3h-variant veertien dagen. Het delete-plan noemt dit al als buiten-scope onder het bredere gat in servicemonitoring; het hoort hier als eigen taak genoemd te worden, niet stil meegenomen.

### 6. `PruneLast=true` beslissen

Zie V1. Dit is de enige stap die de faalmodus bij de wortel weghaalt, en de enige waarvoor we een antwoord nodig hebben dat niet in de repo staat.

## Wat erbuiten valt

De waker die 45 minuten lang 401 bleef kloppen (711 keer, op `/api/sleep-mode/dd-mco/pr-68/status` en `/wake`, na 18 keer 200 daarvoor). Dat is een gevolg: dezelfde taak die de waker-manifests uit git haalde maakte zijn token ongeldig, en omdat de prune nooit liep bleef de pod draaien. Eigen issue waard.

De dode variabele `generated_at` in `project_manager.py` (twee plekken, met een commentaar dat nog de oude bedoeling claimt). De pod-annotatie die hij voedde is op 02-02-2026 vervangen door de config-hash uit de CMP. Opruimwerk van een paar regels, geen gedragsverandering.

Waarom `Deployment/pr-68-website` vóór 15:30 al afweek van git. Niet te achterhalen: de Application is weg en de logregels die het hadden kunnen zeggen gaan over auto-sync, niet over de vergelijking.

## Open vragen

Deze lijst is het werk, niet de bijlage. V1 tot V4 en V9 zijn te meten op de sandbox, V7 en V8 zijn een ontwerpkeuze met een afweging, en V5 en V6 zijn al beantwoord en staan er alleen nog om te laten zien waarmee. Per vraag staat erbij wat als antwoord geldt.

**V1. Is `PruneLast=true` echt nodig? Er is nu een concrete hypothese, en die is te testen.** De optie kwam binnen met de eerste commit van de repo (`2a38a0cae`, 08-10-2025), zonder toelichting, en twee bestaande documenten noemen hem al een open vraag. De algemene motivatie upstream (#5080) is kustomize `configMapGenerator`/`secretGenerator` en die geldt niet voor ons: onze secretnamen staan vast en de inhoudswijziging loopt via `checksum/config`.

Maar er is een tweede vorm die wél precies past, en dat is de PVC-generatie. `generate_pvc_name` (`opi/utils/naming.py:178`) zet de generatie in de naam, dus `frontend-webapp-data-pvc-v1`. Een restore berekent `next_generation` (`opi/api/restore_router.py:942`) en `PVCManager` verwijdert daarna het oude manifest uit git: "Deleting old PVC manifest: ... (generation 1 < 2)" (`opi/manager/pvc_manager.py:75`). In één sync staat dan zowel het aanmaken van `-pvc-v2` en een Deployment die daarnaar wijst, als het prunen van `-pvc-v1` die op dat moment nog gemount kan zijn.

Dat is exact het geval waarvoor te vroeg prunen gevaarlijk is, en het verklaart ook waarom `PrunePropagationPolicy=foreground` ernaast staat. Kubernetes houdt een PVC die nog in gebruik is in `Terminating` met de `kubernetes.io/pvc-protection` finalizer, en met foreground wacht ArgoCD op die verwijdering.

Wat dit afzwakt en dus deel van de test moet zijn: ZAD zet `strategy: Recreate` zodra er persistente storage is (`project_manager.py`, `pod_replacement_mode`), dus de oude pod gaat weg vóór de nieuwe start. Daarmee zou de prune van v1 zich alsnog oplossen zodra die pod verdwenen is, ook zonder `PruneLast`. Of dat echt zo loopt, en of de sync er in de tussentijd op blijft wachten, is niet gemeten.

Het antwoord hierop is dus geen archiefonderzoek meer maar een proef, zie vorm C hieronder. Blijkt dat de PVC-wissel zonder `PruneLast` breekt, dan blijft hij staan en zijn stap 1 tot 4 de oplossing. Blijkt hij te houden, dan halen we de faalmodus bij de wortel weg, voor deze storing én voor de delete uit #184.

**V2. Hoe breken we een sync af die te lang duurt, en is de controller-flag genoeg?** De flag werkt netjes: hij zet `Terminating` en laat de sync zelf afronden, dus met een phase en een `finishedAt`. OPI's crowbar (`remove /operation`) laat juist een losgekoppelde `operationState` achter. De vraag is of we beide willen, met de flag als normale weg en OPI als vangnet voor standen die de flag niet raakt (zoals de #29636-vorm), of dat we één weg willen. Voorstel: beide, want de flag kan niet per Application en OPI weet dingen die de controller niet weet.

*Wat geldt als antwoord*: zet de flag in de sandbox, maak vorm A, en meet of de operation echt eindigt. Kijk naar de phase, naar `finishedAt`, en of `app.Operation` daarna weg is. Doe daarna hetzelfde geval met OPI's terminate in plaats van de flag, en vergelijk welke stand er achterblijft. De uitkomst is een tabel met twee rijen.

**V3. Is een halve apply acceptabel?** Een afgebroken sync laat een deel toegepast en de prune niet gedaan. Voor ons is dat goed, want de volgende sync maakt het af, maar het is wel een bewuste keuze: de Application staat dan korter of langer `OutOfSync` met resources die niet meer in git staan. Willen we dat zo, of moet OPI na een afbreking altijd een nieuwe sync forceren?

*Wat geldt als antwoord*: na een afgebroken sync, wat staat er dan werkelijk in het cluster, wat zegt `describe`, en rondt de eerstvolgende sync het af zonder dat iemand iets doet? Blijft er iets staan dat niet meer in git hoort, zeg dan hoe lang en waardoor het alsnog verdween.

**V4. Moet OPI ook `status.operationState` wissen?** Uit de broncode blijkt dat niets een losgekoppelde `operationState` opruimt. #29636 herstelde met `remove /status/operationState`. Dat is krachtiger en risicovoller: je wist de uitkomst van de laatste uitrol, en daarmee wat `describe` erover kan zeggen. Te verifiëren: of een gewone patch het statusveld van een Application mag wijzigen, of dat daar `--subresource=status` voor nodig is.

*Wat geldt als antwoord*: probeer het op de sandbox, met en zonder `--subresource=status`, en schrijf op welke van de twee werkt. Besluit daarna of OPI het moet doen, met als maatstaf of `describe` erna nog kan zeggen wat de laatste uitrol deed.

**V5. Welke waarde voor `--sync-timeout`? BESLIST: 600.** Tien minuten is de bovengrens die een uitrol mag duren. Hij geldt clusterbreed en voor élke Application, ook de platform-Applications, dus de resterende toets is of er een gezonde uitrol bestaat die langer duurt dan tien minuten. Komt die er, dan is dat een eigen probleem en geen reden om de grens te verhogen.

**V6. Mogen wij de controller-args op odcn-production zetten? BEANTWOORD: ja, en we doen het al.** De CR heeft een `spec.controller.extraCommandArgs` met `--repo-server-timeout-seconds=180`, en die flag staat ook in het command van de draaiende controller-pod. De tenant-afscherming zit dit dus niet in de weg en de route is in productie bewezen.

**V7. Moeten de waker-manifests wel uit git?** De goedkoopste manier om de trigger weg te nemen is hem niet te maken: laat de waker in git staan en zet hem op nul replicas in plaats van hem te verwijderen. Dan valt er bij een wake niets te prunen. Dat raakt sleep-mode, niet ArgoCD, en het lost alleen de meest voorkomende trigger op, niet de andere vier. Wel of niet doen hangt af van V1: halen we `PruneLast` weg, dan is dit niet meer nodig.

**V8. Hoe voorkomen we een volgend blok dat niets doet?** De vergissing staat zeven keer in hetzelfde blok en bleef negen maanden staan omdat de meeste waarden bij toeval gelijk waren aan de default. Dat is geen kwestie van beter opletten: het is onzichtbaar met het blote oog, want je moet per key weten welke ConfigMap hem leest en of de operator hem doorgeeft. Een guard kan dat wel. Voorstel, in de lijn van `tests/test_argocd_generated_application_syncoptions.py` en `tests/test_config_hash_label.py`: toets elke key uit `extraConfig` en `cmdParams` in onze overlays tegen een lijst van keys die daar werkelijk thuishoren, en laat de test falen op een cmd-params-key in `extraConfig`. Open: waar die lijst vandaan komt, want hem met de hand bijhouden is hoe deze vergissing ook ontstond.

**V9. Staat er nog meer stil te niets doen?** Deze storing is gevonden door één instelling na te trekken, en er bleek een heel blok dood te zijn. Van de tien keys werken er drie, en `reposerver.parallelism.limit` is de enige dode die nog een echt effect zou hebben gehad. Dat is klein, maar de vraag erachter is het niet: dezelfde vergissing kan elders in de bootstrap staan. V8 is de structurele kant daarvan.

*Wat geldt als antwoord*: loop de drie overlays (`odcn-production`, `sandboxed-local`, `local`) langs en noem per key of hij aankomt. Meet in de sandbox of `reposerver.parallelism.limit` effect heeft, want dat is de enige dode key met een mogelijk echt gevolg.

## Hoe je weet dat het werkt

Naspelen op de sandbox, en in de vorm die het probleem echt bevat. `orch ship` kan dit in het sandboxcluster draaien. Het bestaande document heeft al een opstelling (`ErrImagePull` plus een ConfigMap op wave 5), maar die mist de prune, en de prune is hier de oorzaak.

**Vorm A, een pending prune naast een ongezonde pod.** Een deployment met twee componenten. Maak component A ongezond (een image-tag die niet bestaat, of een readiness-pad dat 404 geeft). Haal daarna component B weg, zodat zijn resources prune-kandidaten worden. Verwacht: de sync blijft `Running` op `waiting for healthy state of apps/Deployment/<A>`, en een volgende wijziging aan A landt niet. Dat is de storing, nagemaakt zonder sleep-mode erbij.

**Vorm B, dezelfde weg als op 5 oktober.** Sleep-mode aan met `waker: true` en `wake-mode: confirm`, laten slapen, component kapot maken, en wekken. De wake haalt de drie waker-manifests uit git en levert dezelfde drie prune-taken.

**Vorm C, de PVC-generatie, en dit is de proef die V1 beslist.** Een component met persistente storage, een backup, en dan een restore zodat de generatie van `-pvc-v1` naar `-pvc-v2` gaat. Draai die restore twee keer: één keer met `PruneLast=true` en één keer zonder, en kijk per keer naar drie dingen. Komt de nieuwe pod op `-pvc-v2`? Wordt `-pvc-v1` werkelijk verwijderd, of blijft hij in `Terminating` met de `pvc-protection` finalizer? En blijft de sync daarop wachten, of rondt hij af?

Doe die proef ook in de onaangename variant, want daar zit het risico: laat de nieuwe pod niet starten (een storage-class die niet bindt, of een kapot image) en kijk of de oude PVC dan alsnog weg kan. Gaat er in die stand data verloren of raakt een PVC onverwijderbaar, dan is dat het antwoord op V1 en blijft `PruneLast` staan. Een PVC komt niet terug, dus deze proef hoort op een sandbox-project met wegwerpdata.

Let op hoe je `PruneLast` voor de proef uitzet. Met de hand de `syncOptions` van één Application patchen werkt niet: die Application staat zelf in de argo-user-applications-repo en wordt door de `user-applications`-Application met `selfHeal` teruggezet. De eerlijke route is de regel in `manifests/argocd-application.yaml.jinja` op een branch weghalen en die OPI naar de sandbox brengen, zodat de gegenereerde Applications de nieuwe vorm krijgen. Dat raakt dan alle deployments op de sandbox, en dat is daar precies de bedoeling.

Op beide vormen hoort na stap 1 te gelden: de sync eindigt binnen de ingestelde grens met `operation is terminating due to timeout`, en de eerstvolgende commit rolt uit. Na stap 2: OPI laat na zijn eigen time-out geen `Running` operation achter. Na stap 4: één `refresh` is genoeg om een nieuwe pod met de nieuwe waarde te krijgen, zonder de deployment weg te gooien.

En de toets die het echte doel meet, op beide vormen: repareer de oorzaak in A terwijl A nog niet ready is, en controleer dat er een nieuwe pod komt. Lukt dat, dan werkt ArgoCD voor ons.
