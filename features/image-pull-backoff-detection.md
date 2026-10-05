# Een image die niet opgehaald kan worden

## Wat het is

Detectie van componenten waarvan de container image niet op te halen is, en wat OPI
daarmee doet: melden, tellen en alarmeren. Niet meer: uitschakelen.

Tot 2 oktober 2026 zette OPI zo'n component op nul replicas. Dat is weg (RC-243). Wat
blijft is een pod in ImagePullBackOff, die kubelet met zijn eigen backoff gewoon blijft
proberen en die vanzelf omhoog komt zodra de image er is.

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

De kale telling staat er altijd omdat een metriek die alleen bestaat als er iets stuk is,
niet te onderscheiden is van een metriek die niet geleverd wordt. Het label heet
`project_namespace` en niet `namespace`: de scrape voegt zelf een `namespace` toe en een
botsing wordt stil omgedoopt naar `exported_namespace`.

Nooit de kubelet-melding of de image-tag als label. Die zijn onbegrensd.

`ZadComponentKanImageNietOphalen` alarmeert als de kale telling een kwartier boven nul
staat. Een kwartier, want een PR-image die net gebouwd wordt zit daarbinnen.

Een mislukte ronde laat de vorige stand staan in plaats van nul te melden: "we konden niet
kijken" is niet "er is niets aan de hand", en nul is wat het alarm als gezond leest.

Een periodieke LIST en niet de pod-watch van `oom-pod-watch.md`, om twee redenen waarvan de
tweede beslist: die watch staat op elk cluster uit tot hij zich bewezen heeft, en dit moet
overal werken; en een momentopname die opnieuw geteld wordt kan geen verouderde regel
dragen, terwijl een stream die een `DELETED` mist blijft alarmeren op een pod die er niet
meer is.

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
| `opi/handlers/project_file_handler.py` | `classify_image_pull_failure`, `image_is_confirmed_absent`, `IMAGE_PULL_REASONS` |
| `opi/services/image_pull_report.py` | de telling, de momentopname en wat de kaart ervan leest |
| `opi/core/metrics.py` | de twee gauges |
| `bootstrap/rig-system/.../prometheusrule-image-pull.yaml` | het alarm |
| `opi/templates_lotc/bg/_argocd-deployment-card.html.j2` | het blok op de deploymentkaart |
| `opi/services/event_interpreter.py` | de tekst per fout op de detailpagina |
| `opi/services/oom_watcher.py` | de fire-and-forget check die het meldt |
| `opi/manager/project_manager.py` | het uitrolpad dat het meldt |
| `opi/api/resource_router.py` | de sanitize, die het component overslaat |

## Verwant

- [oom-pod-watch.md](oom-pod-watch.md) - OOM-kills tijdens runtime, die wel remedieren
- [auto-resource-tuning.md](auto-resource-tuning.md) - de tuner die een OOM-disable opheft
- [redeploy-clears-recorded-state.md](redeploy-clears-recorded-state.md) - de haak die elke disable opruimt bij een uitrol
