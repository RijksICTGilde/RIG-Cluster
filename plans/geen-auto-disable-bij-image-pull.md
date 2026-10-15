# Niet meer uitschakelen bij image-pull

## Waarom

OPI zet een component op nul replicas als een pod niet kan pullen. Die beslissing valt op één momentopname van één foutstring: `oom_watcher` leest `state.waiting.message` en vraagt `image_is_confirmed_absent()` of het register gezegd heeft dat de image er niet is. Er is geen duur, geen tweede observatie, geen geschiedenis.

Die string is in zeven weken drie keer verkeerd gelezen, elke keer gevonden door een storing:

| datum | wat er gebeurde | de reparatie |
|---|---|---|
| 12-08-2026 | mirror gaf HTTP 500 | 5xx en rate limits op de lijst |
| 10-09-2026 | transport viel om (`EOF`, TLS-timeout, `context deadline exceeded`) | lijst omgedraaid van denylist naar allowlist |
| 30-09-2026 | Quay hergebruikte `DENIED` voor een vol quotum | veto vóór de allowlist (`d4d9969d7`) |

De eerste twee lieten zien dat je niet kunt opsommen hoe een register stuk kan gaan. De derde liet zien dat de omgekeerde lijst overmatcht. Beide richtingen zijn nu een keer fout geweest, dus het probleem is niet welke lijst het is, maar dat één foutmelding als bewijs gebruikt wordt voor een ingreep die de pod weghaalt die het opnieuw zou proberen.

Op 30 september zette dat ongeveer 40 componenten in zes projecten uit terwijl elke image gewoon upstream stond. Negen daarvan stonden twee dagen later nog uit, want een disable lost zichzelf niet op: hij wacht op een rollout.

Wat uitschakelen oplevert is netheid. Dat staat niet in verhouding tot een storing die zijn eigen oorzaak overleeft, en het commentaar van de guard zegt het zelf al: kubelet probeert het met backoff gewoon opnieuw, en een pod in ImagePullBackOff is onschadelijk.

## Waar dit plan tegen gemeten is

Alle regelnummers hieronder verwijzen naar `forgejo/main` op `032c09cdb`, de merge van RC-237 van 2 oktober. Die taak bouwde een pod-watch voor OOM-detectie tijdens runtime en liet `oom_watcher.py` met 672 regels groeien, maar raakte het image-pull-pad niet aan: de diff bevat geen enkele regel met `image_pull`, `ImagePull` of `confirmed_absent`. Het is dus geen overlap, maar het verschuift wel elk regelnummer in dat bestand, en de watch-structuur is de context waarin deze wijziging landt. Lees `features/oom-pod-watch.md` voordat je aan taak 1 begint.


## Het uitgangspunt

De classificatie wordt niet verwijderd, hij wordt gedegradeerd. `image_is_confirmed_absent()` mag blijven bepalen hoe een melding geformuleerd wordt, en mag niet meer bepalen of er iets naar nul gaat. Een heuristiek in een zin is prima; een heuristiek die een pod weghaalt niet.

Wat NIET verandert, expliciet:

- uitschakelen bij OOMKilled blijft, inclusief `is_oom_disable_reason` en de resource-tuner die zo'n disable opheft
- uitschakelen bij CrashLoopBackOff blijft
- het `disabled` plus `disabled-reason` mechanisme in het projectbestand blijft, met dezelfde vorm
- de REDEPLOY-haak in `deployment_health` blijft elke disable opruimen bij een rollout

## De taken

### 1. De drie beslispunten die uitschakelen

`opi/services/oom_watcher.py:830-846`. De detectie en de logregel blijven, de tak die `disable_components_for_image_pull` aanroept vervalt, en daarmee ook de `_queue_refresh_task` die erachteraan komt.

`opi/manager/project_manager.py:3700-3770`. De splitsing in `image_pull_failures` en `registry_down_failures` vervalt: alle image-pull-fouten gaan dezelfde kant op, de rapportagekant. De `disable_components_for_image_pull`-aanroep op 3762 en de import op 3370 gaan weg.

`opi/api/resource_router.py:231-258`. Hier zit een subtiliteit die geen regressie mag worden. Dit pad schakelt componenten uit om meerdere redenen (restarts, niet ready, crash loops) en de image-pull-check doet er twee dingen: hij voegt een reden toe, en hij zet `registry_never_answered` dat de hele sanitize voor dit component overslaat. Dat tweede moet blijven en wordt sterker: een pull die niet lukt verklaart elk ander symptoom in die lijst, want de container is nooit gestart. De regel wordt dus "zie je een image-pull-event, sla dit component helemaal over", onvoorwaardelijk, in plaats van alleen bij een niet-gediagnosticeerde pull.

→ verify: `uv run pytest tests/test_oom_watcher.py tests/test_registry_outage_no_disable.py -q` groen, en geen enkele aanroep van `disable_components_for_image_pull` meer in `opi/` (`grep -rn` leeg).

### 2. De rapportage die ervoor in de plaats komt

`opi/services/event_interpreter.py:242,278` blijft ongewijzigd. Dit is de enige consument die de classificatie om de goede reden gebruikt: hij kiest de tekst die een afnemer op de detailpagina ziet. Zit de heuristiek ernaast, dan leest iemand een iets minder precieze zin, en dat is het hele gevolg.

De WARNING uit `project_manager` (`Runtime pod-health issue(s) after sync (app crashing, not a deploy failure)`) blijft en blijft de volledige kubelet-melding dragen. Dat is de regel waarmee de storing van 30 september te reconstrueren was.

Besloten: een component die niet kan pullen moet **expliciet en zichtbaar** op de deploymentkaart staan. Dit is geen open punt en geen bijzaak, het is de tegenprestatie voor het weghalen van de ingreep. Zonder dit ruilen we een storing die zichzelf verbergt in voor een pod die stilletjes blijft hangen, en dat is geen verbetering.

De kaart leest vandaag `deployment.components | selectattr('disabled')` (`opi/templates_lotc/bg/_argocd-deployment-card.html.j2:41`). Die bron valt weg voor image-pull, dus de kaart moet aansluiten op de pod-health die er al is in plaats van op de `disabled`-vlag. Wat er te zien moet zijn: welk component het is, welke image hij niet kan ophalen, wat het register antwoordde, en sinds wanneer. Die laatste twee komen uit dezelfde `PodHealthResult` die de watcher al vult (`image_pull_image`, `image_pull_error`).

De `disabled`-tak op die kaart blijft bestaan, want OOM en crash-loop blijven wel uitschakelen. Er komen dus twee toestanden naast elkaar te staan en die moeten van elkaar te onderscheiden zijn: uitgeschakeld door OPI, tegenover draait niet maar blijft het proberen.

Drie bestaande gevallen uit taak 3 zijn hiervan de maatstaf, want precies die worden straks hangende pods in plaats van een schone nul: `ai-3rt/api-frontend` (image zonder tag), `toets-hn7/pr-22-frontend` (tag bestaat niet meer) en `tva-d62/productie-frontend` (image `github.io/test/niet`). Dat laatste is een onzin-verwijzing in een deployment die `productie` heet. Zijn die drie na deze wijziging niet expliciet terug te vinden in de UI, dan is taak 2 niet klaar.

→ verify: een testgeval dat een image-pull-fout door de interpreter haalt en de verwachte tekst oplevert; een golden-manifest of templatetest die aantoont dat de kaart het component noemt zonder dat `disabled` gezet is; en die drie gevallen op de detailpagina van hun project terugvinden.

### 3. Poort: controleren of er nog disables staan

Hier is een aanname gecorrigeerd. In de code ruimt **niets** een image-pull-disable automatisch op. De resource-tuner doet het wel voor OOM maar poort expliciet op de reden (`resource_tuning_service.py:1090`, `if is_disabled and is_oom_disable_reason(disabled_reason)`), dus een image-pull-disable raakt hij niet aan. Het enige dat zo'n disable opheft is de REDEPLOY-haak, en die gaat alleen af bij een rollout.

Wat dat betekent: de negen van 30 september (`docs/redeploy-na-quota-storing-20260930.md`) zijn weg als er sindsdien iets op ze is uitgerold, en staan er anders nog. Eén rollout per deployment is genoeg, van hun eigen CI of met het runbook.

Zodra dit plan uitgevoerd is ontstaan er geen nieuwe image-pull-disables meer, dus dit is geen migratie maar een eenmalige controle. De sweep die ze vindt is al bewezen: deployments op nul replicas zonder waker, afgezet tegen `is_image_pull_disable_reason` op de reden in het projectbestand.

De poort blijft staan voor het geval de controle iets vindt: eerst een dry-run die per project en deployment opsomt welke componenten het zou aanzetten en met welke reden, uitvoer naar stdout, niets geschreven. Die lijst gaat langs een mens. Pas daarna schrijven, en dan via de bestaande save-poort van het projectbestand en niet via een eigen YAML-pad.

**Gedraaid op 5 oktober over alle 60 `rig-prd-*` namespaces: deze taak heeft geen werk.** Geen van de negen van 30 september staat nog uit. Zeven deployments staan op nul replicas zonder waker, en geen daarvan is een slachtoffer van die storing:

| namespace | deployment | image | beoordeling |
|---|---|---|---|
| rig-prd-ai-3rt | api-frontend | `par-dpia-form/experiment/backend`, zonder tag | register weigert, plausibel een image-pull-disable |
| rig-prd-toets-hn7 | pr-22-frontend | `toetsingskader-archiefwet:pr-22` | tag bestaat niet meer, plausibel een image-pull-disable |
| rig-prd-tva-d62 | productie-frontend | `github.io/test/niet` | onzin-verwijzing, plausibel `InvalidImageName` |
| rig-prd-amt-odc | amt-odc-component-1 | `minbzk/amt:main` | image is in orde, dus een andere reden |
| rig-prd-waggl-9et | staging-backend | pullt goed | idem |
| rig-prd-waggl-9et | staging-frontend | pullt goed | idem |
| rig-prd-operations | kube-state-metrics | n.v.t. | geen ZAD-component |

De reden uit het projectbestand is hier niet gelezen, dus "plausibel" is geen vaststelling: OPI logt de reden alleen bij het verwerken van een deployment en deze drie zijn in 24 uur niet aangeraakt.

→ verify: gedaan. Mocht de sweep later alsnog iets vinden, dan geldt de poort hierboven.

### 4. Zien dat het gebeurt, in plaats van het opruimen

Zonder auto-disable is een niet-pullende component iets dat blijft hangen, en dat moet zichtbaar zijn. Dit is het stuk dat de ingreep vervangt.

Een gauge in de bestaande collector (`opi/core/metrics.py`, die draait op `GaugeMetricFamily`) voor image-pull-fouten, gelabeld met namespace en een reden-klasse die OPI toch al bepaalt: `absent`, `capacity`, `undiagnosed`. Plus een PrometheusRule in `rig-prd-operations`, waar er al twee staan, die alarmeert als die gauge langer dan een kwartier boven nul staat.

Let op de cardinaliteit: namespace maal reden-klasse is klein, maar neem nooit de kubelet-melding of de image-tag als label op. Die zijn onbegrensd.

→ verify: `curl` op het metrics-endpoint van OPI in de sandbox toont de serie met een nul-waarde bij een gezonde vloot en een één zodra een component naar een niet-bestaande tag wijst; `promtool check rules` op de nieuwe regel.

### 5. Wat echt dood gaat

`disable_components_for_image_pull` in `oom_watcher.py:622` verliest al zijn aanroepers en gaat weg, met zijn twee patches in `tests/test_oom_watcher.py:986,1017`.

`image_is_confirmed_absent` en `_IMAGE_ABSENT_MARKERS` en `_REGISTRY_CAPACITY_MARKERS` blijven staan, want `event_interpreter` gebruikt ze. `tests/test_registry_outage_no_disable.py` blijft ook staan maar verandert van betekenis: het dekt niet meer af dat er niet uitgeschakeld wordt, maar dat de melding goed geclassificeerd wordt. De module-docstring daar moet dus herschreven worden, anders beschrijft hij gedrag dat niet meer bestaat.

`is_image_pull_disable_reason` blijft nodig voor taak 3 en daarna voor de REDEPLOY-haak, die nog steeds oude disables tegenkomt.

→ verify: `uv run ruff check .` en `uv run pyright` schoon, en niets verwijderd dat buiten deze taken nog een aanroeper heeft.

### 6. Het gevolg dat buiten de code valt

Nu gaat een niet-pullende component naar nul replicas en meldt ArgoCD de applicatie als Healthy. Straks blijft de pod in ImagePullBackOff staan en meldt ArgoCD Degraded. Dat is eerlijker, maar het is wel een zichtbare verandering voor afnemers en voor alles wat op applicatie-health kijkt.

Te doen voordat dit uitgaat: nalopen of `gitops-operator-argocd-alerts` in `rig-prd-operations` op Degraded alarmeert, want dan gaat dat alarm straks vaker af. En bij de release vermelden, want een team dat zijn app ineens Degraded ziet staan hoort te weten waarom.

→ verify: de regels in die PrometheusRule gelezen en in de release-notitie benoemd.

## Volgorde

Taak 1 en 2 horen bij elkaar en gaan samen. Taak 5 volgt er direct op, want die ruimt op wat 1 achterlaat. Taak 3 kan parallel, want hij raakt geen code die 1 aanpast. Taak 4 kan los en mag later. Taak 6 is leeswerk dat vóór de release moet.

## Wat hier niet in zit

De storing die dit blootlegde had een tweede oorzaak aan de registerkant: het quotum op de gedeelde proxy-cache-organisatie `ghcr-rig` liep vol, en de vraag waar het niet-gerefereerde volume blijft hangen staat nog open bij ODC-Noord. Dat is geen OPI-werk en hoort niet in deze ship.

De alternatieve vorm die is afgewezen: uitschakelen behouden maar op duur in plaats van op tekst, dus pas na N observaties over T minuten. Dat lost dezelfde storing op maar houdt het mechanisme en daarmee de vraag wanneer het weer net niet klopt. Gekozen is de klasse opruimen.
