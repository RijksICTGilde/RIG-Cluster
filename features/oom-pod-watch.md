# OOM-kills tijdens runtime: de pod-watch

## Wat het is

Een cluster-brede watch op alle applicatiepods die dit cluster draait. Valt een container
om op OOMKilled, dan gaat die kill binnen enkele seconden hetzelfde tune-pad in als een
OOM vlak na een uitrol: de memory-limit omhoog in het projectbestand, committen, refresh.

Dit vult het gat van [de fire-and-forget watcher](oom-kill-watcher.md), die alleen in een
kort venster na een rollout kijkt.

## Waarom

Op 29 september 2026 crashloopte `regel-k4c/pr1644` een half uur op OOMKilled. Niemand
had het door; gebruikers kregen 503's. De pod draaide al dagen, dus het venster van de
fire-and-forget watcher was allang dicht en de enige detectie die overbleef was de
nachtelijke auto-tune via VPA-targets: toevallig, en pas de volgende ochtend.

## Het feit dat het ontwerp stuurt

**Kubernetes kent geen Event voor een OOM-kill.** Tijdens datzelfde incident gemeten: vijf
OOM-kills, nul events. Op de sandbox nagemeten op 29 september: een container die vier keer
op OOM omviel leverde 27 events op, geen daarvan met OOM in de `reason`. De informatie zit
in de pod-status, in `containerStatuses[].lastState.terminated` (`reason: OOMKilled`,
`exitCode: 137`, `startedAt`, `finishedAt`).

Daarom leest deze watch pods, net als kwatch, Robusta en de OOM-observer van de
VPA-recommender. Alleen kube-state-metrics maakt er een metric van, en die is het vangnet,
niet de detectie.

## Bekende grens: een limit die te laag is om te starten

Is de limit zo laag dat de container-init zelf al wordt omgelegd, dan meldt de runtime
`reason: StartError` met exitCode 128 en de tekst "container init was OOM-killed" in de
message, niet `OOMKilled`. Deze watch pikt dat niet op, net zomin als `check_pod_health`
dat doet. Op de sandbox gemeten met een limit van 32Mi.

Dat is een gat, maar het dichten is niet gratis: het zou betekenen dat we op een substring
van een runtime-message gaan triggeren, en die tekst verschilt per container-runtime. Zo'n
deployment komt bovendien nooit Healthy, dus het deployment-health-pad ziet hem wel.

## Hoe het werkt

```
kubectl get pods -A -l component=application,!zad-role --watch --output-watch-events -o json
        |
        +-- ADDED    -> restartCount per pod+container in de cache. Meldt niets.
        |               (dat is ook de beginstand bij opstarten en na een reconnect)
        +-- MODIFIED -> restartCount GESTEGEN?
        |                 nee  -> niets
        |                 ja   -> lastState.terminated.reason == OOMKilled?
        |                           nee  -> logregel, geen actie
        |                           ja   -> dedupe op finishedAt
        |                                   pod -> project/deployment/component
        |                                   pod-generatiegrendel
        |                                   budgetrem
        |                                   apply_oom_tune()
        +-- DELETED  -> pod uit de cache
```

### De trigger is een gestegen restartCount, niet een terminated state

`lastState` blijft dezelfde oude kill beschrijven zolang de pod leeft. Wie op de
aanwezigheid ervan triggert, meldt die ene kill bij elke volgende update van de pod
opnieuw. De stijging van `restartCount` is het moment zelf.

Een container die voor het eerst wordt gezien wordt alleen opgeslagen, nooit gemeld.
Zonder dat zou elke pod die ooit is omgevallen vuren zodra OPI start.

### Alleen OOMKilled

Andere redenen (Error, Completed) hebben hun eigen pad: de image-pull-melding en de
`deployment-health`-dienst. Ze hier ook oppakken zou dezelfde storing twee keer
remedieren.

### Kosten

De stream kost nul requests bij stilte en een pod-object per wijziging. De volle lijst
wordt alleen opgehaald bij het starten en na een verbroken stream. Lineair in wijzigingen,
niet in pods.

## Reconnect

De stream eindigt bij elke hik van de API-server. Dan: backoff (5s, verdubbelend tot
120s), en opnieuw starten. `kubectl get --watch` doet zelf een LIST voor hij gaat
streamen, dus het hervatten is een relist en er is geen `resourceVersion` om bij te
houden.

**De cache overleeft de reconnect.** Dat is geen detail: de relist speelt elke pod opnieuw
af, dus een container waarvan `restartCount` tijdens de onderbreking is opgelopen komt er
alsnog uit als een stijging tegen de onthouden waarde. Werd de cache geleegd, dan was die
kill "de eerste waarneming" en verdween hij.

Een stream die nul events levert telt als een mislukte stream: alleen een stream die echt
liep zet de backoff terug. Zonder dat zou een weggenomen RBAC-recht een lus van vijf
seconden opleveren.

Bij een verlopen watch stuurt de API-server geen pod maar een `Status`
(`{"type":"ERROR", ...}`, `reason: Expired`, `code: 410`) en kubectl geeft die door op
stdout. Dat event beeindigt de ronde en telt niet mee: zou het meetellen, dan was een
stream die alleen een fout leverde "een stream die liep" en kwam de watch elke vijf
seconden terug met een volle cluster-brede LIST, precies de lus die het plafond van 120s
moet voorkomen.

## De remmen

De remmen uit [oom-kill-watcher.md](oom-kill-watcher.md) gelden ongewijzigd, want de
remediatie loopt via dezelfde functie (`apply_oom_tune`). Daar komt er een bij: de dedupe
op `finishedAt`, die dezelfde kill een keer verwerkt, ook over een relist, een
OPI-herstart en het vangnet heen.

De dedupe registreert een kill zodra hij GELEZEN is, niet zodra hij getuned is. Een kill
waar we bewust niets mee doen moet niet opnieuw langskomen, en een kill waarvan de tune
mislukte wordt niet op hetzelfde bewijs opnieuw geprobeerd. De volgende kill draagt een
nieuwe `finishedAt` en krijgt zijn eigen kans.

## Van pod naar component

De labels staan al op alles wat `manifests/deployment.yaml.jinja` rendert: `project`,
`deployment`, `app` (de unique name) en `component: application`. De watch leest die en
zoekt de component op door `generate_unique_name(deployment, reference)` te vergelijken
met het `app`-label, dezelfde richting als de rest van de module.

Daarna twee controles die niets met labels te maken hebben:

- de deployment moet `cluster` van DEZE OPI hebben (geen enkele instantie beheert
  resources op een ander cluster);
- de namespace van de pod moet zijn wat het projectbestand zegt. Het label zegt van wie de
  pod is, het projectbestand waar hij hoort te staan.

Een pod die geen component claimt (helmfile-deployments zoals grist) levert een logregel
op en verder niets: hun manifests komen niet van OPI, dus er valt in het projectbestand
niets te verhogen.

## Het vangnet op de metric

Elk uur een query op de metrics-backend:

```promql
kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace=~"rig-prd-.*"}
```

Het namespace-patroon is het prefix van dit cluster (`get_namespace_prefix`), dus
`rig-prd-` op productie en `rig-` op de sandbox.

Dat dekt wat de watch niet kon zien: de seconden rond een OPI-uitrol, en een kill op een
pod die nog niet in de cache stond.

**De grens.** Het vangnet leest elke hit terug van de pod zelf, dus het ziet alleen kills
waarvan de pod NOG BESTAAT. Is de pod voor de volgende ronde vervangen, door een uitrol of
door de tune zelf, dan valt die kill buiten beide detecties. Om diezelfde reden is de query
hierboven een momentopname en geen `max_over_time` zoals de zusterquery van de tuner (de
afweging achter dat bereik staat in `tests/test_oom_query_venster.py`): een bereik levert
hier alleen rijen op die de verificatie daarna weggooit.

**De metric beslist niets.** Een hit wordt eerst tegen de pod-status zelf geverifieerd, en
`finishedAt` komt daar vandaan, zodat een metric-hit en een watch-event over dezelfde kill
tegen elkaar dedupen. Dat is niet overdreven voorzichtig: de metric meldt kills met
exitcode 137 ook wel als `Error`, en hij is EXPERIMENTAL in kube-state-metrics en kan dus
zonder waarschuwing veranderen of verdwijnen. De watch blijft primair; valt het vangnet
weg, dan logt het dat en loopt de watch door.

**Ook dienst-pods vallen af.** De metric noemt alleen namespace, pod en container, dus de
labelselector die de watch de service-eigen pods laat overslaan doet hier niets. De waker
van sleep-mode draagt met opzet de `app`-, `deployment`- en `project`-labels van het
component dat hij vervangt en draait op een vaste 64Mi; zonder uitsluiting zou zijn OOM de
limit van het echte component verhogen. Beide wegen bouwen hun kill daarom via
`build_oom_kill`, en die toetst `is_application_pod` (`component=application` aanwezig en
`zad-role` afwezig: dezelfde twee helften als de selector).

Op een cluster waar geen metrics-backend antwoordt logt de sweep eenmaal per ronde dat hij
uitgeschakeld is, en doet verder niets.

## Configuratie

| Waar | Sleutel | Standaard |
|---|---|---|
| `cluster_config.py` | `oom_pod_watch` | `False` op elk cluster |
| `config.py` | `OOM_METRIC_SWEEP_INTERVAL_SECONDS` | `3600` |

De watch gaat per cluster aan, niet door gemerged te worden: hij houdt een lange stream
over alle pods open en remedieert zelf. `watches_pods_for_oom(cluster)` is de enige lezer.

## RBAC

De weg naar de pods verschilt per cluster, en dat is precies wat stap 6 van het plan (de
vlag aanzetten op odcn-production) moet weten.

Op `local` en `sandboxed-local` loopt hij via de ClusterRole van OPI
(`bootstrap/rig-system/kustomize/operations-manager/overlays/<cluster>/cluster-role.yaml`).
Die heeft `pods: [create, get, list, delete, watch]` cluster-breed, en een watch gebruikt
dezelfde rechten als een list, dus daar is niets extra's nodig. Zo is de watch ook
nagemeten.

Op `odcn-production` bestaat die ClusterRole niet. Het `_blueprint-cluster-role.yaml` in
die overlay is een blueprint die de kustomization niet in `resources` noemt; de rechten
komen daar per tenant-namespace van een RoleBinding die Capsule aanmaakt. OPI's kubectl
praat er bovendien niet met de API-server zelf: de overlay zet `KUBERNETES_SERVICE_HOST`
op het Capsule-Proxy-endpoint (`overlays/odcn-production/patches/deployment.yaml`). Dat is
dezelfde route die Prometheus daar gebruikt voor zijn pod-discovery, watch-stream en al,
zie [capsule-proxy-prometheus-discovery.md](capsule-proxy-prometheus-discovery.md).

**Nog niet nagemeten:** of een cluster-brede pod-watch via Capsule Proxy doorkomt onder die
per-namespace RoleBinding. De meting in de PR is op de sandbox gedaan, waar de ClusterRole
de enige weg is. Wie de vlag op odcn-production aanzet, begint hier.

## Bestanden

| Bestand | Rol |
|---|---|
| `opi/services/oom_watcher.py` | `OomPodWatcher`, `OomMetricSweeper`, `observe_pod_restarts`, `apply_oom_tune` |
| `opi/connectors/kubectl.py` | `watch_pods()`: het streamende subprocess |
| `opi/services/catalog/base.py` | `all_application_pods_selector()`, `is_application_pod()` |
| `opi/core/cluster_config.py` | `watches_pods_for_oom()` |
| `opi/server.py` | start en stop in de lifespan |
| `tests/test_oom_pod_watch.py` | de toetsen |

## Bewust buiten scope

- Notificaties naar projecteigenaren.
- Automatisch tunen van helmfile-deployments: hun manifests komen niet van OPI.
- `node-problem-detector` en Alertmanager-rules van ODCN: platformafhankelijk, geen
  tenant-pad.

## Bekende grens: meer dan een replica

Draait OPI ooit met meer dan een replica, dan kijken twee watches naar dezelfde pods. De
dedupe op `finishedAt` en de budgetrem vangen het meeste op, maar bij de invoering van HA
hoort hier leader-election bij.

## Zie ook

- [oom-kill-watcher.md](oom-kill-watcher.md) - de fire-and-forget check na een uitrol, en
  de remmen die beide paden delen
- [auto-resource-tuning.md](auto-resource-tuning.md) - de tune zelf
