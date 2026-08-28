# De draaiende pod tonen, en de logs per pod kunnen kiezen

De deploymentkaart zegt sinds RC-162 welke pod er op dit moment verkeer afhandelt, en het
logpaneel laat je kiezen van welke pod je de logs leest - inclusief die van een vorige,
gecrashte poging.

## Waar dit uit voortkomt

Op 21 augustus 2026 meldde een gebruiker dat de deployment `psd-law/pr-114` op productie
niet uit te rollen was. De pagina toonde `Degraded` met de melding "Applicatie crasht
herhaaldelijk" en de suggestie "De container start steeds opnieuw op en crasht. Bekijk de
logs voor de oorzaak."

De clusterstand op dat moment, met read-only kubectl gemeten:

```
pr-114-profielservice-58cb9567c5-9t87d   0/1   CrashLoopBackOff   5 restarts   6m46s
pr-114-profielservice-849d475c4-4qp6p    1/1   Running            0 restarts   3d6h

deployment pr-114-profielservice   READY 1/1   UP-TO-DATE 1   AVAILABLE 1
```

De applicatie was helemaal niet plat. De pod uit ReplicaSet `849d475c4` bediende sinds
18 augustus 11:59 UTC gewoon verkeer op image-digest `sha256:25ab6344`, terwijl de nieuwe
pod uit `58cb9567c5` al negentien uur probeerde op te komen met digest `sha256:2c0728ed`.
De ArgoCD-applicatie stond op `health=Degraded, sync=Synced`, met als enige
resource-melding "Deployment pr-114-profielservice exceeded its progress deadline".

De crash zelf zat in de applicatie: Flyway weigerde te starten op een checksum-mismatch
van migratie 4 plus een toegepaste migratie 5 die niet meer in het image zat. Dat is werk
voor het projectteam. Wat het platform verkeerd deed, is de gebruiker laten geloven dat
zijn applicatie eruit lag.

## Wat er nu op de kaart staat

Boven de foutenlijst, zodat je eerst leest wat er draait en dan pas het alarm, staat per
component een regel:

> profielservice draait sinds 18 augustus 2026 13:59 op
> ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2

Draait die pod een andere image dan er in het projectbestand staat, dan komt daar een zin
achter die dat zegt en de ingestelde image noemt. Die uitspraak wordt alleen gedaan
wanneer beide verwijzingen van dezelfde soort zijn - allebei met digest of allebei met
tag. Een digest tegenover een tag zegt niets over gelijkheid, dus daar komt geen verdict,
alleen de image zelf.

Is er voor een component geen bedienende pod, dan staat er een rode melding "Er draait
niets voor `<component>`". Dan ligt dat deel van de applicatie er wel echt uit.

De digest wordt afgekort tot twaalf tekens. De volle 64 passen niet op een regel en zeggen
niet meer dan het begin. Dat afkorten doet het filter `korte_digest`
(`shorten_image_digest` in `opi/core/template_helpers.py`) en niet een `[:N]` in het
sjabloon: de kaart toont twee imageverwijzingen -- de draaiende en de ingestelde -- dus
anders stond die grens op twee plekken. Een verwijzing met een tag blijft heel.

### Waarom RollingUpdate en Recreate een ander bericht krijgen

Welke van de twee situaties je hebt, ligt al in de code vast: `project_manager.py` kiest
`Recreate` zodra een component persistente opslag heeft en anders `RollingUpdate`.

| situatie | wat er waar is | wat de kaart zegt |
|---|---|---|
| `RollingUpdate`, geen persistent volume | de vorige versie draait door, alleen de nieuwe komt niet omhoog | de draairegel, plus "Nieuwe versie start niet op" |
| `Recreate`, met persistent volume | er draait niets, de applicatie is plat | de rode melding "Er draait niets voor ..." |

De kaart leidt dat niet af uit de strategie maar uit de pods zelf: is er een pod die
bedient, dan is het het eerste geval, en anders het tweede. Dat is dezelfde vraag maar dan
aan het cluster gesteld, en die blijft kloppen als de strategie ooit anders gekozen wordt.

### De crashmelding wordt bijgesteld

Staat er voor hetzelfde component een bedienende pod, dan is "Applicatie crasht
herhaaldelijk" met "Bekijk de logs voor de oorzaak" niet onwaar maar wel misleidend. De
melding wordt dan:

> **Nieuwe versie start niet op** - De vorige versie draait door, dus je applicatie blijft
> bereikbaar. Alleen de nieuwe pod komt niet omhoog; de oorzaak staat in de logs van die
> pod. Kies hem in het logpaneel, en zet 'vorige poging' aan als hij tussen twee pogingen
> in niets laat zien.

Dat gebeurt in `_suppress_symptoms` in `opi/services/event_interpreter.py`, waar de
probe-kill al zo'n correctie doet: een preciezere waarneming verdringt of verscherpt een
minder precieze. Het is bewust geen tweede mechanisme ernaast, en het gebeurt pas nadat de
onderdrukking haar besluiten heeft genomen - anders ziet die stap geen crashtitel meer en
blijft "Deployment duurt te lang" er als tweede melding naast staan.

## Wanneer het verschijnt, en waarom alleen dan

De podinformatie wordt **alleen bij een niet-gezonde kaart** opgehaald, in dezelfde tak die
al de duurdere diagnostiek draait (`_fetch_argocd_deployment_status` in
`opi/web/router.py`). Een gezonde deployment kost geen enkele extra aanroep: die zegt met
zijn groene badge al dat het goed gaat, en de vraag "welke pod dan" komt pas op wanneer dat
antwoord niet meer volstaat.

Hij wordt ook overgeslagen wanneer de deploymentstand `expects_no_application_pods` meldt
(`opi/services/deployment_state.py`). Een slapende of uitgeschakelde deployment hoort geen
pods te hebben, en "er draait niets" is daar geen storing maar de bedoeling. Componenten
die het projectbestand als `disabled` markeert vallen om dezelfde reden buiten de
samenvatting - de kaart noemt die al apart, met hun reden.

## De podselectie in de logstroom

`kubectl logs -f -l app=<naam>` volgt **elke** matchende pod, en zonder `--prefix` staat er
niet bij welke regel bij welke pod hoort. Bij `pr-114` liepen de regels van de draaiende
pod en die van de crashende pod dus ongescheiden door elkaar: je kon de crashende pod niet
kiezen en hem ook niet herkennen.

In het logpaneel staat daarom naast de componentkiezer een **podkiezer**:

- **Alle pods** als eerste optie. Dat is de label-selector, en dus het gedrag van voor
  deze functie.
- Daaronder elke pod, met een leesbaar label: `...-9t87d, start niet, 5 herstarten` en
  `...-4qp6p, draait sinds 18 aug`. Alleen het staartje van de naam, want dat is het enige
  dat de pods onderling onderscheidt.

De lijst komt van `GET /api/logs/pods/{project}?deployment=&component=`, bij het openen en
opnieuw bij elke componentwissel. Standaard staat de kiezer op de **niet-gerede** pod als
die er is - dat is de pod die niet opkomt, en dus de enige waarvoor je logs opent - en
anders op "Alle pods".

Elke draairegel op de deploymentkaart heeft daarnaast een eigen knop **Logs**, die het
paneel meteen op díe pod opent. De bestaande knop "Logs bekijken" blijft staan en blijft op
"Alle pods" openen.

### De autorisatie staat op één plek

Een podnaam komt van de client en mag nooit rechtstreeks in een kubectl-commando belanden.
`kubectl logs <pod>` kijkt niet naar welk component een pod hoort, en een projectnamespace
draagt de deployments van het hele team - zonder grendel zou een lid met een geraden naam
de logs van een collega kunnen meelezen.

`resolve_component_pods` in `opi/api/logs_router.py` is het enige antwoord op "welke pods
mag dit project voor dit component lezen". Zowel het endpoint hierboven als de WebSocket
vraagt het daar, zodat er geen twee antwoorden op dezelfde vraag kunnen ontstaan. De
WebSocket toetst de naam ertegen **bij het openen én bij `switch`**: een verbinding die met
een geldige pod opende en daarna een andere naam meestuurt is precies de weg die je
overhoudt als je alleen het openen toetst.

Het ad-hoc-podpad (de database-console en jobs, herkenbaar aan het `rig.zad/run`-label)
krijgt geen podselectie en is ongewijzigd: dat is al één pod, aangesproken op zijn eigen
label.

## De vorige poging, en waarom dat geen lopende stroom is

Een pod in `CrashLoopBackOff` zit tussen twee pogingen in een backoff-venster van minuten.
`kubectl logs -f` levert dan **niets** tot de volgende poging - je kijkt op precies het
verkeerde moment tegen een leeg scherm aan. De Flyway-fout die de crash van `pr-114`
verklaarde stond in de vorige poging en was alleen met `--previous` te lezen.

De schakelaar **Vorige poging** zet die stand aan. Hij is alleen bedienbaar wanneer er één
pod gekozen is die zo'n poging heeft - `--previous` geldt per container, dus op "Alle pods"
bestaat de vraag niet - en staat standaard aan wanneer de gekozen pod niet gereed is en
herstarts heeft. Dat is precies het geval waarin de live stroom leeg blijft.

Twee eigenschappen van deze stand bepalen het gedrag:

1. **Er valt niets te volgen.** De container is gestopt, de API levert het opgeslagen
   logboek en het proces eindigt meteen. De herverbindlus in `logs_websocket_router.py`
   draait daarom niet in deze stand; zou hij dat wel doen, dan startte hij elke paar
   seconden hetzelfde proces opnieuw en dumpte hij telkens dezelfde tekst. En daarom staat
   in de statusregel dat dit een **afgesloten logboek** is dat niet meer groeit: zonder die
   zin leest een stilstaand paneel als een kapotte verbinding, en gaat iemand een storing
   zoeken die er niet is.

2. **Is er geen vorige poging**, dan antwoordt de server met
   `Error from server (BadRequest): previous terminated container "app" in pod "<naam>" not
   found` (gemeten op productie, 28 augustus 2026). Die tekst belandt niet rauw in het
   paneel: er komt een leesbare melding, en de stroom valt terug op de gewone stand.

## Waar het staat

| onderdeel | plek |
|---|---|
| de pods van een deployment opvragen | `KubectlConnector.get_application_pods`, `opi/connectors/kubectl.py` |
| de selectors, en de naam van de app-container | `opi/services/catalog/base.py` (`deployment_pod_selector`, `APPLICATION_CONTAINER_NAME`) |
| samenvatten wat er draait | `summarize_component_pods`, `opi/services/deployment_diagnostics.py` |
| aan de kaart hangen | `_fetch_argocd_deployment_status`, `opi/web/router.py` |
| het blok op de kaart | `opi/templates_lotc/bg/_argocd-deployment-card.html.j2` |
| de bijgestelde crashmelding | `opi/services/event_interpreter.py` |
| de podlijst en de autorisatie | `opi/api/logs_router.py` |
| logs van één pod, en van de vorige poging | `KubectlConnector.stream_deployment_logs`, `opi/connectors/kubectl.py` |
| de WebSocket en de grendel | `opi/api/logs_websocket_router.py` |
| de kiezer en de schakelaar | `opi/templates_lotc/bg/_log-viewer.html.j2`, `static/js/log_viewer.js` |

Tests: `tests/test_get_application_pods.py`, `tests/test_deployment_diagnostics.py`,
`tests/test_argocd_card_running_pods.py`, `tests/test_event_interpreter.py`,
`tests/test_fetch_argocd_status.py`, `tests/test_logs_pod_selection.py`,
`tests/test_logs_websocket_router.py` en `tests/e2e/test_logviewer_gedrag.py`.
