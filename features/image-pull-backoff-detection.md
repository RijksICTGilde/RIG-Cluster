# Een image die niet opgehaald kan worden

## Wat het is

Detectie van componenten waarvan de container image niet op te halen is, en wat OPI
daarmee doet: melden, tellen en alarmeren. Niet meer: uitschakelen.

OPI zette zo'n component op nul replicas. Dat is weg (RC-243). Wat blijft is een pod in
ImagePullBackOff, die kubelet met zijn eigen backoff blijft proberen en die vanzelf omhoog
komt zodra de image er is.

## Waarom het uitschakelen weg is

De beslissing viel op een momentopname van een foutstring. Die string is in zeven weken
drie keer verkeerd gelezen, elke keer gevonden door een storing:

| datum | wat er gebeurde | wat er toen gerepareerd is |
|---|---|---|
| 12-08-2026 | de mirror gaf HTTP 500 | 5xx en rate limits op de lijst |
| 10-09-2026 | het transport viel om (`EOF`, TLS-timeout, `context deadline exceeded`) | lijst omgedraaid van denylist naar allowlist |
| 30-09-2026 | Quay hergebruikte `DENIED` voor een vol quotum | veto voor de allowlist |

De eerste twee lieten zien dat je niet kunt opsommen hoe een registry stuk kan gaan. De
derde liet zien dat de omgekeerde lijst overmatcht. Beide richtingen zijn nu een keer fout
geweest, dus het probleem was niet welke lijst het is: het was dat een enkele foutmelding
als bewijs gold voor een ingreep die juist de pod weghaalt die het opnieuw zou proberen.

Op 30 september zette dat ongeveer 40 componenten in zes projecten uit terwijl elke image
gewoon upstream stond. Negen daarvan stonden twee dagen later nog uit, want een disable
lost zichzelf niet op: hij wacht op een uitrol.

Wat het uitschakelen opleverde was netheid. Dat staat niet in verhouding tot een storing
die zijn eigen oorzaak overleeft.

## De classificatie is gebleven, gedegradeerd

`classify_image_pull_failure()` in `opi/handlers/project_file_handler.py` leest de
kubelet-melding en geeft een van drie klassen terug. Hij bepaalt hoe iets geformuleerd en
geteld wordt, en nooit meer of er iets naar nul gaat. Een heuristiek in een zin is prima;
een heuristiek die een pod weghaalt niet.

| klasse | wat de registry zei | wie kan handelen |
|---|---|---|
| `absent` | de image of tag is er niet, of mag niet opgehaald worden | het team zelf |
| `capacity` | de gedeelde proxy-cache zit vol (`quota has been exceeded`) | het platform |
| `undiagnosed` | geen antwoord: een 5xx, een timeout, een dode TLS-endpoint, een formulering die nog niemand heeft gezien | het platform, tenzij het blijft staan |

`image_is_confirmed_absent()` is diezelfde functie, versmald tot een antwoord. De
`capacity`-veto staat voor de allowlist, want Quay antwoordt een vol quotum met de
distributiespec-code `DENIED` en `denied` moet een echte weigering kunnen blijven betekenen.

De melding die hij classificeert komt uit een enkele lezer,
`read_image_pull_from_statuses()` in hetzelfde bestand. Beide producenten gebruiken die:
de pod-health-check op het uitrolpad en de telling hieronder. Dat moet, want het prefix
`"{reden}: {kubelet-tekst}"` is dragend. `InvalidImageName` zet namelijk geen tekst die
iets over de image zegt, dus alleen het reden-woord maakt die vorm `absent`; een tweede
lezer die het net anders formuleert laat dezelfde storing op de kaart als `absent` en in de
log als `undiagnosed` landen. De pod-grendels (is dit onze pod, wordt hij opgeruimd, is het
de huidige generatie) blijven per aanroeper, want die stellen een andere vraag.

## Waar het zichtbaar is

### Op de deploymentkaart

Een component dat niet kan pullen staat expliciet op de kaart van zijn deployment, met het
component, de image (in de spelling van de bronregistry), wat de registry antwoordde en
sinds wanneer. Gegroepeerd op reden-klasse, want dat is het enige wat per component
verschilt aan het antwoord op "en wat doe ik eraan".

Dat staat naast, en nadrukkelijk niet in, de lijst met uitgeschakelde componenten: OOM en
crash loops schakelen nog wel uit, en die componenten zijn weg tot er een uitrol komt.
Deze staan aan en proberen het zelf opnieuw.

De gegevens komen uit de telling hieronder, dus de kaart kost er geen clusteraanroep voor.

### Als metriek en alarm

`opi/services/image_pull_report.py` telt elke minuut (`IMAGE_PULL_OBSERVE_INTERVAL_SECONDS`)
met een `kubectl get pods --all-namespaces` hoeveel applicatiepods hun image niet kunnen
ophalen. De collector in `opi/core/metrics.py` geeft dat uit als:

| metriek | labels | wanneer |
|---|---|---|
| `opi_image_pull_failing_pods` | geen | altijd, ook op nul |
| `opi_image_pull_failing_pods_by_reason` | `project_namespace`, `reason` | alleen bij een waarneming |
| `opi_image_pull_observed_timestamp` | geen | altijd; `0` als er nog nooit een ronde lukte |

De kale telling staat er altijd omdat een metriek die alleen bestaat als er iets stuk is,
niet te onderscheiden is van een metriek die niet geleverd wordt. Het label heet
`project_namespace` en niet `namespace`: de scrape voegt zelf een `namespace` toe en een
botsing wordt stil omgedoopt naar `exported_namespace`.

Nooit de kubelet-melding of de image-tag als label. Die zijn onbegrensd.

De regel `ZadComponentKanImageNietOphalen` vuurt als de kale telling een kwartier boven nul
staat. Een kwartier, want een PR-image die net gebouwd wordt zit daarbinnen. Of die regel
ergens geevalueerd wordt is een open punt, hieronder.

### En of er wel gekeken is

Een mislukte ronde laat de vorige stand staan in plaats van nul te melden, want nul is wat
het alarm als gezond leest. Maar voor de eerste gelukte ronde is er geen vorige stand om te
bewaren: een cluster waar de pod-lezing geweigerd wordt meldt dan een schone nul, het alarm
hierboven vuurt nooit en het blok op de deploymentkaart blijft leeg. Dat is dezelfde val
als die deze taak wegneemt, "we konden niet kijken" dat leest als "er is niets aan de hand",
en hij mag niet aan de nieuwe kant terugkomen. Alleen `opi_image_pull_observed_timestamp`
scheidt de twee betekenissen van nul.

De tijdstempel staat op `0` tot de eerste ronde het cluster werkelijk gelezen heeft, en
blijft daarna op het moment van de laatste gelukte ronde staan.
`ZadImagePullObservatieOntbreekt` vuurt als hij ouder is dan tien minuten, dus na tien
mislukte rondes op rij, en daar zit een `for` van vijf minuten op zodat een herstart van
OPI hem niet laat vuren: hij gaat dus af na een kwartier zonder gelukte ronde. Die ene
drempel dekt ook "nog nooit gelukt", want `0` is de epoch en die is ruim langer dan tien
minuten geleden. Dat is niet vanzelfsprekend en het is juist
het geval waar het op `odcn-production` om gaat, dus het staat als toets vast
(`test_image_pull_alarm_leest_de_metriek_die_er_is.py`): een herschrijving naar een vorm
die alleen op verandering kijkt zou precies dat geval laten lopen.

### Een LIST, met een open punt

Een periodieke LIST en niet de pod-watch van `oom-pod-watch.md`, om twee redenen waarvan de
tweede beslist: die watch staat op elk cluster uit tot hij zich bewezen heeft, dus daar is
vandaag niet op te bouwen; en een momentopname die opnieuw geteld wordt kan geen verouderde
regel dragen, terwijl een stream die een `DELETED` mist blijft alarmeren op een pod die er
niet meer is.

Wat die keuze **niet** oplevert is het recht om het cluster te lezen. De LIST is
cluster-breed (`--all-namespaces`), en op `odcn-production` praat OPI's kubectl via Capsule
Proxy onder de RoleBinding die Capsule per tenant-namespace maakt.

**Nog niet nagemeten:** of een cluster-brede pod-lezing daar doorkomt. Dat is hetzelfde
open punt als voor de watch en om dezelfde reden, want een watch gebruikt dezelfde rechten
als een list; zie [oom-pod-watch.md](oom-pod-watch.md). Wie productietoegang heeft, meet
het af voor dit uitgaat, en wel VANUIT de pod:

```bash
kubectl -n rig-prd-operations exec deployment/operations-manager -- \
  kubectl auth can-i list pods --all-namespaces
```

Niet met `--as` van buiten. De overlay zet `KUBERNETES_SERVICE_HOST` op het
Capsule-Proxy-endpoint, dus alleen de kubectl in die pod loopt over de route die OPI
werkelijk neemt; een `can-i` van buitenaf meet de API-server en antwoordt dus over een
andere weg. Op de sandbox is het antwoord `yes`, maar daar is de ClusterRole de enige weg
en zit Capsule Proxy niet in het pad, dus die meting zegt hier niets.

Komt het niet door, dan is dat sinds deze taak te zien in plaats van stil: de tijdstempel
blijft op `0`, en dat is de reeks waar `ZadImagePullObservatieOntbreekt` op staat. Dat die
regel ergens geevalueerd wordt is het open punt hierna.

### Het tweede open punt: draaien die alarmen ergens

Dezelfde keten, een schakel verder. De twee regels worden geleverd als een
`PrometheusRule`, en zo'n CR wordt alleen gelezen door een Prometheus die de
Prometheus-Operator beheert. De `opi_*`-reeksen waar ze op staan bestaan alleen in RIG's
eigen Prometheus: een kale `prom/prometheus` op `prometheus.rig-prd-operations:9090` met
alleen `--config.file`, zonder `rule_files`, `federate` of `remote_read`. Die scrapet OPI
wel (job `kubernetes-pods` op de `prometheus.io/scrape`-annotatie van de OPI-pod) maar
evalueert geen regels, en voor de stack die de regels wel evalueert staat er in deze repo
geen `ServiceMonitor` of `PodMonitor` voor operations-manager.

Dat `prometheusrule-billing.yaml` werkt bewijst het niet: dat is een recording rule over
ODCN-platformreeksen waarvan de uitkomst via Grafana/Mimir terugkomt. Daarmee staat vast dat
ODCN's stack zulke CR's evalueert, niet dat hij OPI's endpoint ziet.

**Nog niet nagemeten:** of een van beide alarmen ooit kan vuren. Meet het van binnenuit,
voordat dit uitgaat:

```bash
kubectl -n rig-prd-operations exec deployment/operations-manager -- \
  wget -qO- http://prometheus.rig-prd-operations:9090/api/v1/rules
```

Staan `ZadComponentKanImageNietOphalen` en `ZadImagePullObservatieOntbreekt` daar niet in,
dan evalueert deze Prometheus ze niet, en is de vervolgvraag of de stack die dat wel doet
`opi_image_pull_failing_pods` heeft.

Twee wegen om het te sluiten, en welke het wordt hangt aan dat antwoord. Laat de evaluerende
stack OPI scrapen: aan onze kant staat daar niets in de weg, want `/metrics` zit niet achter
auth en de NetworkPolicy laat poort 8000 van overal binnen. Of leg de regels in RIG's eigen
Prometheus (`rule_files` plus een rules-ConfigMap in
`infrastructure/bootstrap/infrastructure/prometheus/controller/`); dan evalueren ze zeker,
maar een Alertmanager staat niet in deze repo, dus dan is de volgende vraag waar een vurend
alarm heen gaat.

`test_image_pull_alarm_leest_de_metriek_die_er_is.py` dekt dit niet af en kan dat niet: die
pint de koppeling tussen de regel en de collector, niet of de regel ergens draait. Tot dit
rond is leunt de zichtbaarheid op de deploymentkaart, de gauges zelf en de WARNING's in de
log.

### In de log en in de uitrol

De fire-and-forget check (`oom_watcher`) logt per component een WARNING met de volledige
kubelet-melding en de reden-klasse. Het uitrolpad (`project_manager`) zet elke image-pull-
fout in `health_warnings`, met de volledige melding, achter de regel
`Runtime pod-health issue(s) after sync`. Dat is de regel waarmee de storing van
30 september te reconstrueren was.

Alleen een registry-auth-fout faalt de uitroltaak nog, en alleen als de registry ook echt
antwoord gaf. De auth-markers bevatten kale `401`/`403`, en die kunnen in een image-tag
staan: een transportstoring mag een uitrol niet laten falen op de spelling van een tag.

## Het gevolg dat buiten de code valt

Een niet-pullende component ging naar nul replicas en ArgoCD meldde de applicatie
`Healthy`. Nu blijft de pod staan en meldt ArgoCD `Degraded`. Dat is eerlijker, en het is
een zichtbare verandering voor afnemers en voor alles wat op applicatie-health kijkt.

## De sanitize

`POST /api/resources/{project}/sanitize` schakelt componenten uit om andere redenen
(herstarts, geen enkele ready pod, crash loops). Ziet hij een image-pull-event voor een
component, dan slaat hij dat component **helemaal** over.

Die tweede helft is de subtiliteit. Een pull die niet lukt verklaart elk ander symptoom in
die lijst: nul pods zijn ready en de container "herstart" juist omdat de image nooit is
aangekomen. Uitschakelen op die symptomen haalt de pod weg die het opnieuw zou proberen,
en dat is precies de ingreep die hier verdwenen is.

## Oude disables

Het `disabled` plus `disabled-reason` mechanisme blijft, met dezelfde vorm, voor OOM en
crash loops. Een disable die nog uit de oude image-pull-tak komt wordt opgeruimd zoals
altijd: een uitrol vuurt `ActionEvent.REDEPLOY` en de deployment-health-dienst haalt de
disable weg, wat de reden ook zei. Zie `redeploy-clears-recorded-state.md`.

`is_image_pull_disable_reason()` bestaat daarvoor nog, en voor niets anders: er wordt geen
nieuwe image-pull-disable meer geschreven.

Op 5 oktober 2026 is over alle 60 `rig-prd-*` namespaces nagegaan of er nog zulke disables
stonden. De negen van 30 september stonden er niet meer. Zeven deployments stonden op nul
replicas zonder waker, en geen daarvan was een slachtoffer van die storing.

## Belangrijke bestanden

| bestand | waarvoor |
|---|---|
| `opi/handlers/project_file_handler.py` | `read_image_pull_from_statuses`, `classify_image_pull_failure`, `image_is_confirmed_absent`, `IMAGE_PULL_REASONS` |
| `opi/services/image_pull_report.py` | de telling, de momentopname en wat de kaart ervan leest |
| `opi/core/metrics.py` | de drie gauges |
| `bootstrap/rig-system/.../prometheusrule-image-pull.yaml` | de twee alarmen |
| `opi/templates_lotc/bg/_argocd-deployment-card.html.j2` | het blok op de deploymentkaart |
| `opi/services/event_interpreter.py` | de tekst per fout op de detailpagina |
| `opi/services/oom_watcher.py` | de fire-and-forget check die het meldt |
| `opi/manager/project_manager.py` | het uitrolpad dat het meldt |
| `opi/api/resource_router.py` | de sanitize, die het component overslaat |

## Verwant

- [oom-pod-watch.md](oom-pod-watch.md) - OOM-kills tijdens runtime, die wel remedieren
- [auto-resource-tuning.md](auto-resource-tuning.md) - de tuner die een OOM-disable opheft
- [redeploy-clears-recorded-state.md](redeploy-clears-recorded-state.md) - de haak die elke disable opruimt bij een uitrol
