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

## Het uitgangspunt

De classificatie wordt niet verwijderd, hij wordt gedegradeerd. `image_is_confirmed_absent()` mag blijven bepalen hoe een melding geformuleerd wordt, en mag niet meer bepalen of er iets naar nul gaat. Een heuristiek in een zin is prima; een heuristiek die een pod weghaalt niet.

Wat NIET verandert, expliciet:

- uitschakelen bij OOMKilled blijft, inclusief `is_oom_disable_reason` en de resource-tuner die zo'n disable opheft
- uitschakelen bij CrashLoopBackOff blijft
- het `disabled` plus `disabled-reason` mechanisme in het projectbestand blijft, met dezelfde vorm
- de REDEPLOY-haak in `deployment_health` blijft elke disable opruimen bij een rollout

## De taken

### 1. De drie beslispunten die uitschakelen

`opi/services/oom_watcher.py:745-772`. De detectie en de logregel blijven, de tak die `disable_components_for_image_pull` aanroept vervalt, en daarmee ook de `_queue_refresh_task` die erachteraan komt.

`opi/manager/project_manager.py:3700-3770`. De splitsing in `image_pull_failures` en `registry_down_failures` vervalt: alle image-pull-fouten gaan dezelfde kant op, de rapportagekant. De `disable_components_for_image_pull`-aanroep op 3762 en de import op 3370 gaan weg.

`opi/api/resource_router.py:231-258`. Hier zit een subtiliteit die geen regressie mag worden. Dit pad schakelt componenten uit om meerdere redenen (restarts, niet ready, crash loops) en de image-pull-check doet er twee dingen: hij voegt een reden toe, en hij zet `registry_never_answered` dat de hele sanitize voor dit component overslaat. Dat tweede moet blijven en wordt sterker: een pull die niet lukt verklaart elk ander symptoom in die lijst, want de container is nooit gestart. De regel wordt dus "zie je een image-pull-event, sla dit component helemaal over", onvoorwaardelijk, in plaats van alleen bij een niet-gediagnosticeerde pull.

→ verify: `uv run pytest tests/test_oom_watcher.py tests/test_registry_outage_no_disable.py -q` groen, en geen enkele aanroep van `disable_components_for_image_pull` meer in `opi/` (`grep -rn` leeg).

### 2. De rapportage die ervoor in de plaats komt

`opi/services/event_interpreter.py:242,278` blijft ongewijzigd. Dit is de enige consument die de classificatie om de goede reden gebruikt: hij kiest de tekst die een afnemer op de detailpagina ziet. Zit de heuristiek ernaast, dan leest iemand een iets minder precieze zin, en dat is het hele gevolg.

De WARNING uit `project_manager` (`Runtime pod-health issue(s) after sync (app crashing, not a deploy failure)`) blijft en blijft de volledige kubelet-melding dragen. Dat is de regel waarmee de storing van 30 september te reconstrueren was.

Open punt om in de review te beslissen: moet een niet-pullende component ook op de deploymentkaart te zien zijn nu hij niet meer in `disabled_components` belandt? De kaart leest vandaag `deployment.components | selectattr('disabled')`. Valt dat weg, dan verdwijnt het signaal uit de UI terwijl de pod wel blijft hangen. Voorstel: de kaart laten aansluiten op de bestaande pod-health in plaats van op de `disabled`-vlag. Dit is een voorstel en geen besluit; het raakt `_argocd-deployment-card.html.j2` en de diagnostics eronder, en kan een eigen taak worden als het te groot blijkt.

→ verify: een testgeval dat een image-pull-fout door de interpreter haalt en de verwachte tekst oplevert, plus visuele controle op de sandbox met een component dat naar een niet-bestaande tag wijst.

### 3. Poort: de bestaande disables opruimen

Dit is een schrijfweg over projectbestanden van andere teams en hij hoort niet stil te gaan. In de vloot staan nu componenten uitgeschakeld met een image-pull-reden, waaronder de negen van 30 september (`docs/redeploy-na-quota-storing-20260930.md` noemt ze). Die lossen zichzelf niet op.

`is_image_pull_disable_reason()` bestaat al en herkent precies deze disables aan hun reden-prefix, dus de selectie is geen nieuw werk.

De poort: eerst een dry-run die per project en deployment opsomt welke componenten het zou aanzetten en met welke reden ze uitstaan, uitvoer naar stdout, niets geschreven. Die lijst gaat langs een mens. Pas daarna de schrijfactie, en die loopt via de bestaande save-poort van het projectbestand, niet via een eigen YAML-pad.

Open beslissing: hoort dit in `_fixup_v2_data` van de schema-migratie (dan gebeurt het bij de eerstvolgende load van elk projectbestand, vanzelf en zonder scheduler) of in een los script onder `operations-manager/python/scripts/`? De migratie is de plek waar reparaties van bestandsinhoud horen, maar hij draait per project op het moment dat iemand dat project aanraakt, dus de negen blijven staan tot er iets met ze gebeurt. Een script doet de vloot in één keer maar heeft een mens nodig die het start. Mijn voorkeur is de migratie, met het script als duwtje voor de achterblijvers.

→ verify: dry-run op een kopie van de vloot laat exact de componenten zien die `is_image_pull_disable_reason` matcht en geen enkele OOM- of crash-loop-disable; daarna op de sandbox een echte run en de componenten staan weer op één replica.

### 4. Zien dat het gebeurt, in plaats van het opruimen

Zonder auto-disable is een niet-pullende component iets dat blijft hangen, en dat moet zichtbaar zijn. Dit is het stuk dat de ingreep vervangt.

Een gauge in de bestaande collector (`opi/core/metrics.py`, die draait op `GaugeMetricFamily`) voor image-pull-fouten, gelabeld met namespace en een reden-klasse die OPI toch al bepaalt: `absent`, `capacity`, `undiagnosed`. Plus een PrometheusRule in `rig-prd-operations`, waar er al twee staan, die alarmeert als die gauge langer dan een kwartier boven nul staat.

Let op de cardinaliteit: namespace maal reden-klasse is klein, maar neem nooit de kubelet-melding of de image-tag als label op. Die zijn onbegrensd.

→ verify: `curl` op het metrics-endpoint van OPI in de sandbox toont de serie met een nul-waarde bij een gezonde vloot en een één zodra een component naar een niet-bestaande tag wijst; `promtool check rules` op de nieuwe regel.

### 5. Wat echt dood gaat

`disable_components_for_image_pull` in `oom_watcher.py:609` verliest al zijn aanroepers en gaat weg, met zijn twee patches in `tests/test_oom_watcher.py:986,1017`.

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
