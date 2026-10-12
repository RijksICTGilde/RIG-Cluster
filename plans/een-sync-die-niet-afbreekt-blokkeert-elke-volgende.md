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

**Vorm A, een pending prune naast een ongezonde pod.** Een deployment met twee componenten. Maak component A ongezond en haal daarna component B weg, zodat zijn resources prune-kandidaten worden. Verwacht: de sync blijft `Running` op `waiting for healthy state of apps/Deployment/<A>`, en een volgende wijziging aan A landt niet. Dat is de storing, nagemaakt zonder sleep-mode erbij.

> **Gecorrigeerd na meting (5 oktober 2026).** "Een image-tag die niet bestaat" werkt hier NIET, en dat is geen detail maar het verschil tussen de storing en iets anders. Een pod die niet START laat de Deployment na `progressDeadlineSeconds` (default 600s) `Degraded` worden, en dan FAALT de sync-taak: gemeten liep de operation van 20:59:25 om 21:02:30 vast op `one or more synchronization tasks completed unsuccessfully`, en de `retry.limit: 2` van de Application begon om 21:03:10 een NIEUWE operation op de nieuwste revisie. Dat is dus geen onbepaald lange blokkade. De storing van 5 oktober had een pod die wel draaide maar nooit READY werd (readiness 503), en die houdt de Deployment `Progressing`. De proef moet dat nadoen: `progressDeadlineSeconds: 2147483647` plus een readiness-probe die nooit slaagt. Dezelfde correctie geldt voor de opstelling in `features/argocd-vastgelopen-verwijdering.md` (`ErrImagePull` plus een ConfigMap op wave 5).

**Vorm B, dezelfde weg als op 5 oktober.** Sleep-mode aan met `waker: true` en `wake-mode: confirm`, laten slapen, component kapot maken, en wekken. De wake haalt de drie waker-manifests uit git en levert dezelfde drie prune-taken.

**Vorm C, de PVC-generatie, en dit is de proef die V1 beslist.** Een component met persistente storage, een backup, en dan een restore zodat de generatie van `-pvc-v1` naar `-pvc-v2` gaat. Draai die restore twee keer: één keer met `PruneLast=true` en één keer zonder, en kijk per keer naar drie dingen. Komt de nieuwe pod op `-pvc-v2`? Wordt `-pvc-v1` werkelijk verwijderd, of blijft hij in `Terminating` met de `pvc-protection` finalizer? En blijft de sync daarop wachten, of rondt hij af?

Doe die proef ook in de onaangename variant, want daar zit het risico: laat de nieuwe pod niet starten (een storage-class die niet bindt, of een kapot image) en kijk of de oude PVC dan alsnog weg kan. Gaat er in die stand data verloren of raakt een PVC onverwijderbaar, dan is dat het antwoord op V1 en blijft `PruneLast` staan. Een PVC komt niet terug, dus deze proef hoort op een sandbox-project met wegwerpdata.

Let op hoe je `PruneLast` voor de proef uitzet. Met de hand de `syncOptions` van één Application patchen werkt niet: die Application staat zelf in de argo-user-applications-repo en wordt door de `user-applications`-Application met `selfHeal` teruggezet. De eerlijke route is de regel in `manifests/argocd-application.yaml.jinja` op een branch weghalen en die OPI naar de sandbox brengen, zodat de gegenereerde Applications de nieuwe vorm krijgen. Dat raakt dan alle deployments op de sandbox, en dat is daar precies de bedoeling.

Op beide vormen hoort na stap 1 te gelden: de sync eindigt binnen de ingestelde grens met `operation is terminating due to timeout`, en de eerstvolgende commit rolt uit. Na stap 2: OPI laat na zijn eigen time-out geen `Running` operation achter. Na stap 4: één `refresh` is genoeg om een nieuwe pod met de nieuwe waarde te krijgen, zonder de deployment weg te gooien.

En de toets die het echte doel meet, op beide vormen: repareer de oorzaak in A terwijl A nog niet ready is, en controleer dat er een nieuwe pod komt. Lukt dat, dan werkt ArgoCD voor ons.


---

# Antwoorden op de open vragen

Gemeten op 5 oktober 2026 op het sandboxcluster (`kind-rig-sandbox`, ArgoCD
`ghcr.io/minbzk/base-images/argocd-rig:v3.5.1-rig1`, argocd-operator), met de sandbox onder
de lock van deze taak. Alle tijden in UTC.

De opstelling is een eigen Application `rc250-proef` met woordelijk dezelfde `syncOptions`
als een gegenereerde ZAD-Application (`CreateNamespace=false`,
`PrunePropagationPolicy=foreground`, `PruneLast=true`,
`SkipDryRunOnMissingResource=true`), `automated` met `prune` en `selfHeal`, en de
`resources-finalizer`. Geen ZAD-project eromheen, want wat hier gemeten wordt zit in
ArgoCD en niet in OPI; waar OPI's eigen code aan de beurt is, is die rechtstreeks tegen
deze Application gedraaid. Alles is na afloop opgeruimd.

## De storing, nagemaakt

De tasklijst van de vastgelopen operation, uit de controller-log:

```
Sync/0 resource apps/Deployment:rc250-proef/proef-web  obj->obj (Synced,Running,deployment.apps/proef-web configured)
Sync/1 resource /Secret:rc250-proef/proef-extra-token  obj->nil (,,)
Sync/1 resource /ConfigMap:rc250-proef/proef-extra     obj->nil (,,)
```

Dezelfde vorm als op 5 oktober: `obj->nil` prune-taken op een eigen wave ná 0, en op wave 0
een Deployment die niet gezond wordt. En de A/B-meting, twee keer dezelfde Application:

| | tasklijst | uitkomst |
|---|---|---|
| 20:56:30 | alleen wave 0, geen prune | `Succeeded (all tasks run)`, started = finished, **0 seconden**, terwijl de pod niet ready was |
| 20:59:25 | wave 0 plus twee prunes op wave 1 | `Running` op `waiting for healthy state of apps/Deployment/proef-web`, en zo gebleven |

En de blokkade zelf: om 21:01:25 is de "fix" gepusht, en de controller zei op diezelfde
seconde `Skipping auto-sync: another operation is in progress`. De nieuwe revisie landde
niet.

## V1. Is `PruneLast=true` echt nodig? BEANTWOORD: ja, hij blijft staan

Vorm C gedraaid als PVC-generatiewissel, twee keer op dezelfde Application, met als enige
verschil `PruneLast` in de `syncOptions`. De Deployment heeft `strategy: Recreate`, zoals
ZAD die zet zodra er persistente storage is.

**Met `PruneLast=true`, en de nieuwe pod kan starten.** `proef-data-pvc-v1` naar `-v2`:

```
21:16:42  sync start. wave 0: pvc-v2 aanmaken + Deployment bijwerken. wave 1: pvc-v1 prunen
21:16:51  oude pod Terminating (Recreate), pvc-v2 Pending, pvc-v1 nog Bound
21:17:15  nieuwe pod Pending, pvc-v2 nog Pending
21:17:30  pod gescheduled -> pvc-v2 Bound -> pod 1/1 Running
21:17:34  wave 0 gezond -> wave 1 draait -> pvc-v1 gepruned -> "successfully synced (all tasks run)"
```

52 seconden, en in de juiste volgorde: de oude generatie verdwijnt pas als de nieuwe pod
draait.

**Zonder `PruneLast`, en de nieuwe pod kan NIET starten.** Eerst
`echo "kostbare klantdata" > /data/belangrijk.txt` in de pod op `-v2`. Daarna in één commit
`-v3` erbij, de Deployment ernaartoe, `-v2` eruit, en een image dat niet bestaat:

```
21:18:13  push
21:18:27  operation al Succeeded (started = finished), pvc-v2 Terminating, oude pod Terminating
21:18:51  pvc-v2 WEG, zijn PV weg, de data weg. Nieuwe pod ErrImagePull, heeft nooit gedraaid.
          Application meldt Synced.
```

Veertien seconden tussen de push en de verwijdering van de vorige generatie, en ArgoCD
rapporteerde dat als een geslaagde sync.

Dat weerlegt ook de verzachting uit de vraag. `strategy: Recreate` redt je niet, het is
juist wat je kwetsbaar maakt: het haalt de laatste mount weg, waardoor de
`pvc-protection`-finalizer loslaat en de prune meteen doorgaat. `PruneLast` is precies wat
de vorige generatie in leven houdt tot de nieuwe pod gezond is.

Dus: `PruneLast=true` blijft staan, en stap 1 tot 4 zijn de oplossing. De faalmodus blijft
daarmee bestaan, maar hij is nu begrensd (V2) en OPI laat hem niet staan.

*Wat hier NIET gemeten is*: de echte ZAD-restoreweg (`generate_pvc_name`,
`next_generation`, `PVCManager`). Dat vraagt een project met storage, een backup en een
restore op de sandbox, en dat is een eigen dag werk. Wat gemeten is, is het mechanisme
eronder, met dezelfde `syncOptions`, dezelfde `strategy: Recreate` en dezelfde naamvorm
`...-pvc-v1` / `-v2`. Dat de restore die vorm oplevert staat in de code
(`opi/manager/pvc_manager.py:75`); dat die vorm zonder `PruneLast` data kost, staat nu
hierboven.

## V2. Hoe breken we een sync af, en is de controller-flag genoeg? BEANTWOORD: beide, met de flag als normale weg

De flag kwam aan: na een patch op `spec.controller.extraCommandArgs` staat
`--sync-timeout=600` in het command van `argocd-application-controller-0`.

Hij vuurde op de seconde. De operation startte om 21:03:10:

```
21:13:11  Terminating in-progress operation due to timeout. Started at: 2026-10-05 21:03:10 +0000 UTC, timeout: 10m0s
21:13:11  Updating operation state. phase: Terminating -> Failed,
          message: 'operation is terminating due to timeout' -> 'Operation terminated'
21:13:11  Sync operation to 2c99bbd0... failed: Operation terminated, triggered by controller sync timeout
21:13:11  Initiated automated sync to '206ea5f3...'      <- de nieuwste revisie, dus de fix
21:13:11  Skipping auto-sync: application status is Synced
```

De fix stond sinds 21:01 in git en landde zonder dat iemand iets deed. De nieuwe pod draaide
om 21:13:26.

De tabel die de vraag vroeg, met OPI's eigen weg ernaast (de echte
`terminate_stuck_operation` uit deze PR, gedraaid tegen deze Application):

| | controller-flag `--sync-timeout=600` | OPI `remove /operation` |
|---|---|---|
| wanneer | exact 600s na `startedAt` | zodra OPI hem tegenkomt, op of boven de drempel van 300s |
| phase erna | `Terminating` -> `Failed`, bericht `Operation terminated` | blijft `Running` |
| `finishedAt` | gezet | niet gezet |
| `.operation` | weg | weg |
| wat er daarna gebeurt | `Initiated automated sync` naar de nieuwste revisie, binnen dezelfde seconde | geen nieuwe sync op DEZELFDE revisie: `Skipping auto-sync: failed previous sync attempt to [...] and will not retry` |

De flag laat dus de nettere stand achter, en hij trekt de volgende sync er zelf bij. OPI's
weg laat een losgekoppelde `operationState` staan. Dat is geen bezwaar, want OPI beëindigt
VOOR zijn push: de revisie die daarna in git komt is nieuw, en daarmee valt
`alreadyAttemptedSync` weg en loopt auto-sync alsnog. Die volgorde is hiermee geen
smaakkwestie meer maar een meting, en `test_argocd_stuck_sync.py` pint hem.

Allebei dus, met de flag als normale weg en OPI als vangnet voor de standen die de flag
niet raakt.

## V3. Is een halve apply acceptabel? BEANTWOORD: ja, en hij lost zichzelf op

Op het moment van afbreken was de Deployment toegepast en waren de twee prune-taken niet
gedaan: `configmap/proef-extra` en `secret/proef-extra-token` stonden er nog, terwijl ze
niet meer in git staan. De Application leest dan `OutOfSync` met `requiresPruning: true` op
die twee, en de deviatieregel uit stap 3 zegt nu wat er aan de hand is in plaats van "wordt
bij de volgende sync opgeruimd".

Zolang de oorzaak blijft bestaan blijft die stand bestaan: elke volgende sync strandt op
dezelfde wave en wordt na 600s weer afgebroken. Dat is onvermijdelijk, want de prune KAN
niet eerder, en het is ook precies de bedoeling van `PruneLast` (zie V1).

Zodra de oorzaak weg is, is het binnen seconden voorbij, zonder dat iemand iets doet:

```
21:21:02  commit die de readiness-probe repareert
21:21:02  sync start
21:21:04  Succeeded, Synced/Healthy
21:21:11  configmap/proef-extra en secret/proef-extra-token zijn weg
```

Dus nee, OPI hoeft na een afbreking geen sync te forceren. Wat er ligt, ligt er omdat het
nog niet weg KAN, en het verdwijnt vanzelf zodra dat wel kan.

## V4. Moet OPI ook `status.operationState` wissen? BEANTWOORD: nee

Eerst de verificatie die de vraag noemt. De CRD declareert geen status-subresource:

```
$ kubectl get crd applications.argoproj.io -o jsonpath='{range .spec.versions[*]}{.name}{" subresources="}{.subresources}{"\n"}{end}'
v1alpha1 subresources={}

$ kubectl -n rig-system patch application rc250-proef --subresource=status --type=json \
    -p '[{"op":"remove","path":"/status/operationState"}]'
Error from server (NotFound): applications.argoproj.io "rc250-proef" not found

$ kubectl -n rig-system patch application rc250-proef --type=json \
    -p '[{"op":"remove","path":"/status/operationState"}]'
application.argoproj.io/rc250-proef patched
```

Een gewone patch mag het dus wel, en `--subresource=status` niet. Let op de melding: die
zegt dat de Application niet bestaat, terwijl het de subresource is die niet bestaat.

Maar het helpt niet. Direct na die patch had de controller `operationState` al opnieuw
geschreven, met een verse `startedAt` van hetzelfde moment, want `.operation` hing er nog.
Wissen zonder eerst `.operation` weg te halen is dus een no-op.

En erna is het niet nodig. Na OPI's terminate blijft `operationState` op `Running` staan met
`.operation` weg, en auto-sync is dan NIET meer geblokkeerd: de controller kwam langs
`another operation is in progress` heen en sloeg alleen nog over met
`failed previous sync attempt to [<dezelfde revisie>]`. De lock is dus vrij, en dat is waar
het om ging. Bij de eerstvolgende nieuwe revisie loopt hij gewoon (gemeten in V3: 21:21:02).

Maatstaf uit de vraag: kan `describe` erna nog zeggen wat de laatste uitrol deed? Met
wissen niet, zonder wissen wel. Er is dus iets te verliezen en niets te winnen. Niet doen.
Heroverweeg het alleen voor de vorm uit [#29636](https://github.com/argoproj/argo-cd/issues/29636)
(phase `Succeeded` met `syncResult: nil`), en dat is onze zaak niet.

## V5. Welke waarde voor `--sync-timeout`? BESLIST: 600, en nagemeten

De grens vuurde op 600,0 seconden (21:03:10 -> 21:13:11). Ter maat: de gezonde
PVC-generatiewissel uit V1, met een pod die opnieuw moet starten en een PVC die opnieuw moet
binden, duurde 52 seconden.

De resterende toets uit de vraag, of er een gezonde uitrol bestaat die langer dan tien
minuten duurt, is op de sandbox niet te beantwoorden: de platform-Applications daar zijn
kleiner dan op odcn-production. Dat hoort bij het uitrolbesluit voor odcn-production en
staat hieronder bij "Wat hierna nog moet".

## V6. Mogen wij de controller-args op odcn-production zetten? BEANTWOORD: ja

Stond al zo in het plan op grond van `--repo-server-timeout-seconds=180`, en is nu ook
nagemeten: een patch op `spec.controller.extraCommandArgs` levert een nieuwe
controller-pod op met `--sync-timeout=600` in zijn command.

## V7. Moeten de waker-manifests wel uit git? VOORSTEL: niet doen

Dit hing af van V1, en V1 houdt `PruneLast`. De trigger blijft dus bestaan, en de vraag is
of we hem goedkoper wegnemen door de waker in git te laten staan op nul replicas.

De afweging, en hij valt tegen:

* **Wat het oplevert.** Bij een wake valt er niets te prunen, dus de meest voorkomende van
  de vijf triggers verdwijnt. Dat is de trigger die deze storing veroorzaakte.
* **Wat het kost.** Een Deployment, een Secret en een ConfigMap die permanent blijven staan
  voor elke deployment die ooit geslapen heeft. Dat is zichtbaar in elk overzicht, het
  vraagt om uitleg bij elke lezer die zich afvraagt waarom daar een waker staat die niets
  doet, en het waker-token blijft bestaan terwijl het bij een slapende deployment niets
  bewaakt. Bovendien is het een tweede mechanisme naast sleep-mode zelf, met zijn eigen
  randgevallen (wat als iemand de replicas met de hand ophoogt).
* **Wat het niet oplevert.** De andere vier triggers blijven: een component dat uit een
  deployment gaat, een dienst die uitgezet wordt, storage die verdwijnt, en OPI's eigen
  opruimronde van verouderde component-manifests.
* **Wat het overbodig maakt.** Met stap 1 is de blokkade hoogstens tien minuten, en met
  stap 2 beëindigt OPI hem al vóór zijn volgende push. De trigger is daarmee geen storing
  meer maar een vertraging.

Voorstel: niet doen. Heroverweeg het alleen als een meting laat zien dat wakes in de
praktijk een veelvoorkomende bron van tien minuten blokkade zijn; dan is er met de
alarmering uit stap 5 ook een manier om dat te tellen.

## V8. Hoe voorkomen we een volgend blok dat niets doet? GEBOUWD

`operations-manager/python/tests/test_argocd_extra_config_keys.py` loopt elke ArgoCD-CR
onder `bootstrap/rig-system/kustomize/overlays/` langs (gevonden op `kind`, niet op
bestandsnaam) en toetst vier dingen:

1. geen key uit `CMD_PARAMS_KEYS` in `extraConfig` (de vergissing zelf);
2. elke key in `extraConfig` staat in `ARGOCD_CM_KEYS` of onder een bekend prefix (de
   andere richting: een onbekende key is rood);
3. een key in `cmdParams` moet er een zijn die deze operator werkelijk doorgeeft, en dat is
   er precies één (`controller.resource.health.persist`);
4. de drie overlays zijn gedekt, en de walk heeft ook echt keys gelezen.

Vier mutaties gemeten, elk 1 tot 3 rood; de basis is 39 groen samen met de zusterguard.

**Het open deel van de vraag, waar de lijst vandaan komt, eerlijk beantwoord**: hij wordt
met de hand bijgehouden, net als `KNOWN_SYNC_OPTIONS` ernaast. Wat er verandert is niet de
lijst maar de FAALRICHTING. Vandaag kon een nieuwe key er stil bij; met deze guard is een
key die in geen van beide lijsten staat rood, en groen krijgen vraagt dat iemand opschrijft
welke ConfigMap hem leest. Dat is wat "beter opletten" niet kan leveren, want de vergissing
is met het blote oog onzichtbaar.

Een lijst die zichzelf bijhoudt zou upstream moeten komen: ArgoCD levert zijn eigen
`argocd-cmd-params-cm.yaml` met alle keys erin. Zolang we die niet vendoren is hem afleiden
raden, en dan is een handlijst met bewijs eerlijker dan een generator die lijkt te weten wat
hij niet weet.

## V9. Staat er nog meer stil te niets doen? BEANTWOORD per key

Nagemeten op de sandbox, en dat is strenger dan de meting uit het plan op odcn-production:
daar waren zes van de zeven dode keys bij toeval gelijk aan de operator-default, hier niet.

```
$ kubectl -n rig-system get pod argocd-application-controller-0 -o jsonpath='{.spec.containers[0].command}'
argocd-application-controller --operation-processors 10 --redis ... --repo-server ...
  --status-processors 20 --kubectl-parallelism-limit 10 --loglevel info --logformat text
$ kubectl -n rig-system get cm argocd-cmd-params-cm
Error from server (NotFound): configmaps "argocd-cmd-params-cm" not found
```

| key | in `sandboxed-local`/`local` | wat er draaide | oordeel |
|---|---|---|---|
| `timeout.reconciliation` | 6m | 6m | werkt, echte argocd-cm-key |
| `timeout.hard.reconciliation` | 0s | 0s | werkt, idem |
| `resource.customizations.health...` | lua | staat in argocd-cm | werkt, idem |
| `controller.status.processors` | 60 | **20** (operator-default) | dood, en de waarde week af |
| `controller.operation.processors` | 30 | **10** (operator-default) | dood, en de waarde week af |
| `controller.kubectl.parallelism.limit` | 20 | **10** (operator-default) | dood, en de waarde week af |
| `controller.sharding.algorithm` | round-robin | niet gezet | dood, sharding staat uit |
| `controller.repo.server.timeout.seconds` | 180 | geen env, geen flag | dood, **en hier zonder werkende route ernaast** |
| `server.repo.server.timeout.seconds` | 180 | geen `--repo-server-timeout-seconds` op argocd-server | dood, **en hier zonder werkende route ernaast** |
| `reposerver.parallelism.limit` | 20 | geen `--parallelismlimit` op argocd-repo-server | dood |

Twee dingen die het plan nog niet wist:

* Op `sandboxed-local` en `local` staan de twee repo-server-timeouts dood ZONDER de
  werkende route die odcn-production er wel naast heeft (`spec.controller.extraCommandArgs`
  en `.env`, `spec.server.extraCommandArgs`). Daar draaide de repo-server-timeout dus negen
  maanden op de default. Het weghalen van de dode key verandert daar niets aan wat er
  draait, maar het haalt wel de laatste plek weg waar die bedoeling nog stond. Dat is een
  bewuste keuze: hem alsnog aanzetten is een gedragsverandering op een gedeeld cluster die
  niemand gevraagd of gemeten heeft, en die hoort in een eigen besluit.
* `argocd-cmd-params-cm` bestaat op de sandbox helemaal niet, dus daar is ook de
  "verhuizing naar `spec.cmdParams`" dubbel dood.

`reposerver.parallelism.limit` is de enige dode key met een mogelijk echt gevolg: zonder
`--parallelismlimit` doet de repo-server onbeperkt parallelle manifest-generaties in plaats
van 20. Of dat op de sandbox merkbaar is, is NIET gemeten: dat vraagt een belastingproef met
tientallen gelijktijdige renders, en die zegt op een kind-cluster met vier cores weinig over
odcn-production. Wat wel vaststaat is dat de instelling niet aankomt; wie hem wil, zet hem
als `--parallelismlimit` in `spec.repo.extraCommandArgs`.

## Wat er nog niet is gemeten, en waarom

* **Vorm B (sleep-mode met een waker).** Niet gedraaid. Vorm A levert dezelfde drie
  prune-taken op wave 1 op, en dat is het hele mechanisme; vorm B voegt daar alleen
  sleep-mode als trigger aan toe. Met V7 (niets veranderen aan de waker) hangt er geen
  besluit aan.
* **De echte ZAD-restoreweg voor V1.** Zie de kanttekening bij V1.
* **Of er een gezonde uitrol bestaat die langer dan tien minuten duurt.** Zie V5.
* **Of `reposerver.parallelism.limit` effect zou hebben.** Zie V9.
* **Alles achter de inlog.** De sandbox heeft op dit moment lege OIDC-secrets
  (`operations-manager-env-secrets` in `rig-system` heeft beide sleutels leeg), dus er is
  geen werkende SSO. Geen van de metingen hierboven liep door een login: ze gaan via
  `kubectl`, via de ArgoCD-controller-log, en via OPI's eigen code rechtstreeks aangeroepen.
  Lees dit dus niet als "SSO is meegetoetst".

## Wat hierna nog moet

1. **Uitrollen naar odcn-production is een apart besluit.** Deze taak raakt die cluster
   niet. De wijziging staat klaar in de overlay; het uitrollen vraagt een nieuwe
   controller-pod, en de resterende toets uit V5 (bestaat er een gezonde uitrol die langer
   dan tien minuten duurt) hoort daarbij.
2. **Alarmeren** is als eigen taak opgeschreven in
   `features/futures/alarmeren-op-een-sync-of-verwijdering-die-blijft-staan.md`.
3. **De waker die 45 minuten 401 bleef kloppen** staat in dit document al als buiten-scope
   en is nog steeds een eigen issue waard.
