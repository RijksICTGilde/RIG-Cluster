# De tuner en de VPA vertellen verschillende verhalen

**Status**: Onderzoeksplan, nog niets geïmplementeerd
**Aangemaakt**: 2026-08-10
**Aanleiding**: `regel-k4c/editor` op odcn-production — limits sprongen in drie nachten van 1318Mi naar 25Mi naar 961Mi naar 25Mi
**Doel**: auto-tuning voor `regel-k4c` weer aan durven zetten

## Waar dit over gaat

De editor van RegelRecht is handmatig op 128Mi gezet met `auto-tune-resources: false`, omdat de tuner hem afwisselend op 25Mi (te laag om te draaien) en op 961Mi (absurd hoog voor wat die container doet) zette. De opt-out is een noodgreep, geen oplossing: hij schakelt ook het OOM-pad uit, dus als 128Mi te krap blijkt kan niets de editor nog redden.

De vraag die dit plan moet beantwoorden is niet "wat is de juiste waarde voor de editor". Het is: **waarom komen er twee onverenigbare metingen uit hetzelfde systeem, en welke van de twee liegt?**

## Wat er feitelijk gebeurde

Uit de git-historie van `zad-projects`, `projects/regel-k4c.yaml`, kolom = `limits.memory` voor `editor`:

| moment | root (componentdefinitie) | deployment `regelrecht` | commit |
|---|---|---|---|
| 08-07 19:40 | 1318Mi | 1672Mi | (handmatige deployment-update) |
| 08-08 01:02 | **25Mi** | 786Mi | `auto-tune: adjust resources for editor, grafana` |
| 08-09 01:01 | **961Mi** | **25Mi** | `auto-tune: adjust resources for editor, editor, ...` |
| 08-09 01:05 | **25Mi** | 25Mi | `auto-tune: adjust resources for editor` |

Twee dingen springen eruit.

**De root wordt geschreven.** De componentdefinitie — de waarde die de gebruiker declareert — is drie keer door de tuner overschreven. Binnen één nacht zelfs twee keer, om 01:01 en om 01:05.

**De uitslagen zijn niet te verzoenen.** 25Mi en 961Mi zijn geen meetruis om dezelfde waarheid heen; het is factor veertig. De onderbouwing in de history laat zien waar de knip zit:

- `Request: VPA target 641Mi = 641Mi. Limit: VPA target 641Mi x 1.5 = 961Mi`
- `Request: max 13Mi + 25% = 25Mi. Limit: max 13Mi x 1.5 = 25Mi`

De eerste komt uit de VPA, de tweede uit de Prometheus-fallback. Dezelfde container, twee bronnen, geen overlap.

## Wat de code zegt (geverifieerd)

- `resource_tuning_service.py:322` — de VPA wordt alleen gebruikt als `target_memory_mi > VPA_MEMORY_FLOOR_MI` (250). Daaronder valt hij terug op ruwe Prometheus. Dit is een harde drempel zonder hysterese: rond de 250Mi klapt de bron heen en weer, en met de bron verandert de hele rekenwijze.
- `resource_tuning_service.py:264,277` — de Prometheus-kant meet `max_over_time(container_memory_working_set_bytes[24h])`.
- De VPA-recommender meet dezelfde metriek, maar aggregeert over acht dagen en levert een percentiel met veiligheidsmarge, niet een max.
- `container_memory_rss` wordt nergens in `opi/` gebruikt. Er is dus geen enkel signaal dat page cache van echt geheugen scheidt.
- `vpa.yaml.jinja` — één VPA per workload, `targetRef` op de eigen Deployment, selector `app: {deployment}-{component}` en dus uniek. `parse_vpa_status` (`opi/connectors/vpa.py:65`) matcht strikt op `containerName == "app"` zonder fallback. **Aan de VPA-bedrading mankeert niets**; de recommender krijgt de juiste pods.
- `scheduler` draait elke nacht 01:00 Europe/Amsterdam over alle projecten. Er is geen cooldown, bewust niet (`resource_tuning_scheduler.py:13-16`); de deadband (+10% omhoog, −30% omlaag, minimaal 16Mi) moet churn voorkomen. Bij factor-veertig-sprongen doet die deadband niets.

## Bevinding die alles anders maakt

`resource_tuning_service.py:623` schrijft vandaag **uitsluitend** naar het deployment-niveau, met een expliciete toelichting erboven:

> Writing it there was a last-writer-wins race that pulled asses-k2n/api from 75Mi to 45Mi in six seconds.

`set_component_resources` heeft geen enkele productie-aanroeper meer — alleen tests. Die fix is commit `73413d98`, 1 augustus 2026.

**Die commit zit niet in `main`.** `main` staat op `51fd763e` van 27 juli. De fix leeft alleen op feature-branches. Op odcn-production draait dus nog de versie die de root wél overschrijft — precies wat de tabel hierboven op 8 en 9 augustus laat zien.

Dat betekent ook dat de root-vloer uit `resource_tuning_service.py:478` (een deployment-override mag nooit onder de gedeclareerde root) in productie geen bescherming biedt: de root zakt gewoon mee. Dat is een correctie op wat ik eerder aannam.

## Hypothesen, en hoe je ze onderuit haalt

Drie verklaringen, aflopend van "meest waarschijnlijk" naar "minst". Ze sluiten elkaar niet uit.

**H1 — De twee bronnen meten verschillende tijdvensters, en beide hebben gelijk.**
De VPA kijkt acht dagen terug, Prometheus 24 uur. Een piek van vijf dagen geleden zit nog in de VPA-aggregatie en is uit het Prometheus-venster verdwenen. Dan is 641Mi geen leugen maar een oude waarheid, en is 25Mi geen leugen maar een recente. Het systeem kiest alleen willekeurig welke van de twee het vanavond gelooft.
*Falsificatie*: draai `max_over_time(container_memory_working_set_bytes{...}[8d])` naast `[24h]` voor dezelfde pod. Komt de 8d-waarde in de buurt van de VPA-target, dan klopt H1 en is de VPA niet de boosdoener.

**H2 — Page cache blaast de meting op.**
`working_set` is `usage - total_inactive_file`: inactieve cache valt eraf, actieve cache niet. Een container die uit een PVC leest en dezelfde bestanden warm houdt, telt die cache mee als geheugengebruik. De editor heeft `persistent-storage` op `/data` én een postgres-database. Als de RSS van het app-proces rond de 13Mi ligt en `working_set` op 640MB, is het verschil vrijwel volledig cache — geheugen dat de kernel op elk moment weggooit en dat de app dus niet nodig heeft.
*Falsificatie*: `container_memory_rss` naast `container_memory_working_set_bytes` voor dezelfde pod over hetzelfde venster. Grote spread = H2 bevestigd.

**H3 — De VPA-target is grover dan hij oogt.**
Exact 641Mi staat op editor in `upload`, `pr1037`, `pr1138` én `pr1139`, én op `harvester-worker` in `pr1037` — een ander image. Vier identieke getallen tot op de megabyte over verschillende workloads is geen meting. De VPA-recommender gebruikt exponentiële histogrambuckets (rond deze waarde ~5% breed) en geeft de bucketgrens terug. Alles wat in de band 640-670MB valt komt er als 641Mi uit.
Dit maakt de VPA niet fout, maar wel een grover instrument dan de reden-tekst suggereert. `VPA target 641Mi = 641Mi` leest als een precisie die er niet is.
*Falsificatie*: `kubectl get vpa <naam> -n regel-k4c -o json` en kijk naar `lowerBound` / `target` / `upperBound`. Ligt daar een brede band tussen, dan is H3 bevestigd.

Je vermoeden dat de VPA onzin vertelt is dus deels ondersteund (H3 zeker, H2 waarschijnlijk), maar de heftigste schade — de sprong naar 25Mi en het overschrijven van de root — komt níet van de VPA. Die komt van de Prometheus-fallback en van code die in productie ouder is dan de fix.

## Taken

### 1. Meet de grondwaarheid voordat er iets verandert

Voor `regelrecht-editor` in namespace `regel-k4c`, en ter controle voor één PR-deployment die op 641Mi staat:

```
kubectl get vpa regelrecht-editor -n regel-k4c -o json      # target, lowerBound, upperBound
kubectl top pod -n regel-k4c --containers | grep editor
```

En in Prometheus, over dezelfde pod:

```
max_over_time(container_memory_working_set_bytes{namespace="regel-k4c",pod=~"regelrecht-editor.*",container="app"}[24h])
max_over_time(container_memory_working_set_bytes{namespace="regel-k4c",pod=~"regelrecht-editor.*",container="app"}[8d])
max_over_time(container_memory_rss{namespace="regel-k4c",pod=~"regelrecht-editor.*",container="app"}[8d])
```

Leg de vier getallen naast elkaar. Dit beslist H1, H2 en H3 in één keer, en het kost tien minuten. Alles hieronder hangt hiervan af — niet implementeren voordat dit er ligt.

Noteer ook wat de editor eigenlijk ís (statische server, Node-proces, iets anders). Een container die 13Mi RSS haalt is geen applicatie die 640MB nodig heeft, en andersom.

### 2. Zet de root-write fix in productie

Commit `73413d98` moet naar `main` en uitgerold. Zolang dat niet gebeurd is, is elke andere maatregel zinloos: de tuner blijft de gedeclareerde waarde overschrijven en de root-vloer blijft dood.

Controleer daarbij of er meer resource-tuning-fixes op feature-branches zijn blijven liggen — `main` loopt op dit onderdeel twee weken achter en dat is waarschijnlijk niet de enige.

### 3. Leg vast wélke bron een wijziging schreef

Vandaag krijgt elke history-entry `source: auto-tune`; of de waarde uit de VPA of uit Prometheus kwam is alleen af te leiden uit de vrije tekst van `reason`. `analysis.source` bestaat al en gaat al mee in `change_record` (`resource_tuning_service.py:653`) — schrijf hem ook naar de history-entry.

Zonder dit veld is dit onderzoek de volgende keer weer handwerk. Klein, en het maakt de oscillatie meteen zichtbaar in het projectbestand zelf.

### 4. Haal de bronwissel weg

De harde drempel op `VPA_MEMORY_FLOOR_MI` laat het systeem tussen twee rekenwijzes met verschillende vensters én verschillende statistieken springen. Dat is de directe oorzaak van de factor-veertig-sprongen, ongeacht welke bron gelijk heeft.

Richtingen, te kiezen op basis van taak 1:
- **Één bron.** Als de 8d-Prometheus-meting de VPA-target benadert (H1), heb je de VPA voor geheugen niet nodig en kun je op één venster sizen. CPU blijft dan wel VPA-werk.
- **Vensters gelijktrekken.** Zet `window_hours` op de VPA-aggregatieperiode, zodat de fallback niet systematisch lager uitkomt dan de VPA.
- **Hysterese op de wissel.** Wissel pas van bron na N nachten aan dezelfde kant van de drempel.

Leg de keuze voor voordat je bouwt; dit raakt elk project op de cluster, niet alleen `regel-k4c`.

### 5. Bescherm de ondergrens tegen een stille container

`max 13Mi + 25% = 25Mi` op een container die HTTP hoort te serveren is geen meting van een gezonde applicatie, het is een meting van een applicatie die niets deed. De bestaande availability-guard (`resource_tuning_service.py:231`) kijkt naar de `Available`-conditie van de Deployment, en die staat gewoon op True voor een pod die draait maar niets te doen heeft.

Overweeg: verlagen alleen toestaan als de container in het venster aantoonbaar werk deed (verkeer, CPU boven idle), of een absolute ondergrens per component die alleen omlaag mag met bewijs. De cluster-minimum van 25Mi is een runtime-ondergrens, geen verantwoorde applicatiewaarde.

### 6. Zet auto-tuning weer aan voor regel-k4c

Pas ná 1 en 2, en na de maatregel uit 4. Dan:

- `auto-tune-resources: false` van de `editor`-componentdefinitie af
- root op de waarde die uit taak 1 volgt (128Mi als de RSS-meting dat steunt, hoger als dat niet zo is)
- eerst één nacht meekijken op `regel-k4c` alleen, vóór de fleet-brede sweep vertrouwd wordt

Met de fix uit taak 2 actief is de root een vloer die de tuner alleen omhoog mag bijstellen. Dat is precies het gedrag dat je wilt: handmatig een bodem leggen, de tuner het dak laten bepalen.

## Verificatie

- Draai de nachtelijke sweep drie nachten op rij en vergelijk de commits: geen enkele wijziging in de componentdefinitie van welk project dan ook.
- Geen enkele component springt in één nacht meer dan een factor twee.
- Elke `auto-tune`-history-entry noemt zijn bron.
- `regel-k4c/editor` blijft drie nachten stabiel zonder OOMKill en zonder dat de tuner hem aanraakt.

## Open besluiten

**Is 128Mi de goede handmatige waarde?** Onbekend tot taak 1. Vijf actieve deployments staan op 641Mi request. Als dat echt geheugen is, gaat de editor op 128Mi om. Als het cache is, is 128Mi ruim. Dit is de enige vraag waar productie-risico aan hangt.

**Wat doen we met de PR-deployments die nu op 961Mi tot 1400Mi staan?** Die zijn niet aangeraakt. Bij een namespace-quotum reserveren ze samen enkele gigabytes voor omgevingen die grotendeels stilstaan. Na taak 1 in één keer rechttrekken, niet los.

**Moet de opt-out het OOM-pad blijven uitschakelen?** Nu betekent `auto-tune-resources: false` ook "geen redding bij OOM". Dat maakt de veiligste knop tegelijk de gevaarlijkste. Een gesplitste vlag — handmatige sizing, maar OOM mag altijd ophogen — is verdedigbaar, maar het is een apart ontwerp en hoort niet in dit onderzoek thuis.
