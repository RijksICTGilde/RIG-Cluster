# De draaiende pod tonen, en de logs per pod kunnen kiezen

## Wat er gebeurde

Op 21 augustus 2026 meldde een gebruiker dat de deployment `psd-law/pr-114` op productie niet uit te rollen was. De pagina toonde `Degraded` plus de melding "Applicatie crasht herhaaldelijk", met als suggestie "De container start steeds opnieuw op en crasht. Bekijk de logs voor de oorzaak." De vraag die daarop kwam was: welke pod draait er dan wel?

De clusterstand op dat moment, gemeten met read-only kubectl:

```
pr-114-profielservice-58cb9567c5-9t87d   0/1   CrashLoopBackOff   5 restarts   6m46s
pr-114-profielservice-849d475c4-4qp6p    1/1   Running            0 restarts   3d6h

deployment pr-114-profielservice   READY 1/1   UP-TO-DATE 1   AVAILABLE 1
```

De applicatie was dus helemaal niet plat. De pod uit ReplicaSet `849d475c4` bediende sinds 18 augustus 11:59 UTC gewoon verkeer op image-digest `sha256:25ab6344`, terwijl de nieuwe pod uit `58cb9567c5` al negentien uur probeerde op te komen met digest `sha256:2c0728ed`. De ArgoCD-applicatie stond op `health=Degraded, sync=Synced`, met als enige resource-melding "Deployment pr-114-profielservice exceeded its progress deadline".

De crash zelf zat in de applicatie: Flyway weigerde te starten op een checksum-mismatch van migratie 4 plus een toegepaste migratie 5 die niet meer in het image zat. Dat is werk voor het projectteam en niet voor het platform. Wat het platform verkeerd deed, is de gebruiker laten geloven dat zijn applicatie eruit lag.

Let op bij het bouwen: deze pods bestaan niet meer. `pr-114` is inmiddels opgeruimd, dus de situatie is niet meer op productie na te kijken en moet in de sandbox nagebootst worden.

## Wat er ontbreekt

De kaart leest uitsluitend ArgoCD: gezondheid, sync, en de foutmeldingen uit de resource tree en de namespace-events (`opi/web/router.py`, `_fetch_argocd_deployment_status`, en `opi/services/deployment_diagnostics.py`). Geen van die bronnen zegt welke pod op dit moment verkeer afhandelt. Daardoor vallen twee situaties samen die niets met elkaar te maken hebben:

| situatie | wat de kaart zegt | wat waar is |
|---|---|---|
| `RollingUpdate`, component zonder persistent volume | "Applicatie crasht herhaaldelijk" | de vorige versie draait door, alleen de nieuwe komt niet omhoog |
| `Recreate`, component met persistent volume | "Applicatie crasht herhaaldelijk" | er draait niets, de applicatie is plat |

Welke van de twee je hebt, staat al vast in de code: `project_manager.py` rond regel 5868 kiest `Recreate` zodra een component persistente opslag heeft en anders `RollingUpdate`. Voor het eerste geval is de melding onnodig alarmerend, voor het tweede juist te mild.

Het tweede gat zit in de logstroom. `KubectlConnector.stream_deployment_logs` (`opi/connectors/kubectl.py` rond regel 904) start `kubectl logs -f -l app=<unieke naam>`. Een label-selector zonder `--prefix` levert de regels van **alle** matchende pods door elkaar heen, zonder te vermelden welke regel bij welke pod hoort. Bij `pr-114` liepen de regels van de draaiende pod en die van de crashende pod dus ongescheiden door elkaar. Je kon de crashende pod niet kiezen en hem ook niet herkennen.

Daar komt bij dat een pod in `CrashLoopBackOff` tussen twee pogingen in een backoff-venster van minuten zit. `kubectl logs -f` op zo'n pod levert dan niets tot de volgende poging. De Flyway-fout die de crash van `pr-114` verklaarde stond in de vorige poging en was alleen met `--previous` te lezen. Zonder die optie kijk je op precies het verkeerde moment tegen een leeg scherm aan.

## Reikwijdte

De podinformatie verschijnt **alleen bij een niet-gezonde kaart**, dus in dezelfde tak die nu al de duurdere diagnostiek draait. Een gezonde deployment kost geen enkele extra aanroep. Dit is een expliciete keuze van de gebruiker en geen bezuiniging die je mag terugdraaien.

## Taken

### Taak 1: de pods van een deployment opvragen

Bestanden: `opi/connectors/kubectl.py`, `opi/services/catalog/base.py`.

Voeg `KubectlConnector.get_application_pods(namespace: str, deployment_name: str) -> list[dict[str, Any]]` toe. Eén `kubectl get pods -n <ns> -l <selector> -o json`, waarbij de selector `deployment=<deployment_name>,component=application,!<SERVICE_ROLE_LABEL_KEY>` is. Die labels staan op elke applicatiepod, gemeten op productie:

```
{"app":"pr-114-profielservice","component":"application","deployment":"pr-114","project":"psd-law","pod-template-hash":"849d475c4", ...}
```

en worden gezet door `manifests/deployment.yaml.jinja`. De `!zad-role`-uitsluiting houdt pods buiten beeld die een dienst naast de applicatie draait, precies zoals `application_pod_selector` in `opi/services/catalog/base.py` rond regel 237 dat per component doet. Zet de deployment-brede variant naast die functie neer, zodat beide selectors op één plek staan. Naamvoorstel, nog niet vastgelegd: `deployment_pod_selector`.

Geef per pod terug: podnaam, het `app`-label, `pod-template-hash`, of er een `deletionTimestamp` staat, en per containerstatus met naam `app`: `ready`, `image`, `restartCount`, `state.running.startedAt`, en of er een `lastState.terminated` is. Best-effort zoals de rest van de connector: een niet-nul exitcode of onparsebare uitvoer logt en levert een lege lijst, en gooit nooit.

Assertie: een test die de methode voedt met vastgelegde JSON in de vorm van de meting hierboven, dus twee pods voor hetzelfde component waarvan er één ready is met nul herstarts en één niet-ready met vijf herstarts en een `lastState.terminated`, en die controleert dat beide er met de juiste velden uit komen. Plus een test dat een lege of foutieve uitvoer een lege lijst geeft en geen exception.

### Taak 2: samenvatten wat er draait, en dat aan de kaart hangen

Bestanden: `opi/services/deployment_diagnostics.py`, `opi/web/router.py`.

Dit hoort in `deployment_diagnostics.py` en niet in `deployment_observation.py`: dat tweede bestand gaat over de after-sync-remediatie en commit in het projectbestand, en dit is een leesbewerking voor de kaart.

Bouw een functie die uit de podlijst van taak 1 per component een samenvatting maakt. Koppel pod aan component via het `app`-label tegen een vooraf gebouwde map `{generate_unique_name(deployment, ref): ref}` over de componentreferenties uit het projectbestand. Doe dat niet door de deploymentnaam als prefix van het label af te knippen: `generate_unique_name` is niet altijd een simpele samenvoeging, en een verkeerd gekoppelde pod is erger dan geen pod.

Per component:

- De bedienende pod is de pod zonder `deletionTimestamp` waarvan de `app`-container `ready` is. Zijn ze dat geen van allen, dan is er geen bedienende pod, en dat is een eigen uitkomst en niet een lege waarde.
- De image van die pod gaat door `original_image()` uit `opi/extensions/registry_rewrite.py`, met de mappings uit `get_registry_rewrite_mappings(settings.CLUSTER_MANAGER)`. Dat is dezelfde behandeling die `_source_image` in `event_interpreter.py` rond regel 213 al toepast. De gebruiker hoort `ghcr.io/minbzk/...` te zien en niet de proxyvorm `rcr.rijksapps.nl/ghcr-rig/minbzk/...`.
- Draaisinds komt uit `state.running.startedAt` van de `app`-container, niet uit `status.startTime` van de pod: na een herstart is het eerste de waarheid en het tweede niet.
- Vergelijk de image van de bedienende pod met `comp.image` uit het projectbestand, beide genormaliseerd met `original_image()`. Doe die uitspraak **alleen** wanneer de twee verwijzingen van dezelfde soort zijn, dus allebei met digest of allebei met tag. Vergelijk je een digest met een tag, dan zegt ongelijkheid niets en mag er geen verdict komen, alleen de image zelf. `_split_image_reference` in `opi/manager/project_validation.py` rond regel 553 splitst een verwijzing al in repository, tag en wel-of-geen-digest; gebruik die.

Haak de samenvatting aan in `_fetch_argocd_deployment_status` in `opi/web/router.py`, in de tak die nu al `gather_deployment_errors` aanroept, dus uitsluitend wanneer `app_health != "Healthy"`. Sla hem over wanneer de deploymentstand `expects_no_application_pods` meldt (`opi/services/deployment_state.py` regel 31): een slapende of uitgeschakelde deployment hoort geen pods te hebben en "er draait niets" is daar geen storing. Geef het resultaat mee in hetzelfde statusdictionary als `errors` en `deviations`, zodat de kaart er geen tweede weg voor nodig heeft.

Assertie: drie tests op de samenvattingsfunctie, voor draait-de-ingestelde-image, draait-een-andere-image, en draait-niets. Plus een test op `_fetch_argocd_deployment_status` die aantoont dat er bij `health=Healthy` geen podaanroep wordt gedaan, en dat er bij `expects_no_application_pods` ook bij `Degraded` geen wordt gedaan. Die twee zijn de reikwijdte uit dit plan en horen daarom vastgelegd te staan.

### Taak 3: het blok op de deploymentkaart

Bestand: `opi/templates_lotc/bg/_argocd-deployment-card.html.j2`.

Zet het blok boven de foutenlijst, zodat je leest wat er draait vóór je het alarm leest. Twee vormen:

- Er draait iets: een gewone regel per component, bijvoorbeeld "profielservice draait sinds 18 augustus 13:59 op ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344". Kort de digest af tot twaalf tekens, de hele digest is onleesbaar en zegt niet meer. Draait die pod een andere image dan de ingestelde, dan komt daar één zin achter die dat zegt.
- Er draait niets voor dit component: een `c-alert type="error"`, want dan is de applicatie wél plat en dat is een ander bericht dan een mislukte uitrol.

Houd je aan de LOTC-regels uit `ROOS_CLAUDE_REFERENCE.md` en aan de vormgeving die deze kaart al gebruikt, dus `c-stack` en `c-cluster` voor de indeling en geen eigen CSS.

Assertie: een rendertest per vorm, die controleert dat de datum en de bronregistry-image in de uitvoer staan, dat de proxyvorm `rcr.rijksapps.nl` er niet in staat, en dat de geen-pod-vorm als `error` rendert en de wel-pod-vorm niet.

### Taak 4: de crashmelding aanscherpen wanneer de vorige versie doordraait

Bestand: `opi/services/event_interpreter.py`, tests in `tests/test_event_interpreter.py`.

Staat er voor hetzelfde component een bedienende pod, dan is "Applicatie crasht herhaaldelijk" met "Bekijk de logs voor de oorzaak" niet onwaar maar wel misleidend. Maak er in dat geval van dat de nieuwe versie niet opstart, dat de vorige doordraait en de applicatie dus bereikbaar blijft, en dat de logs van de nieuwe pod de oorzaak dragen.

Dit is dezelfde soort correctie als `_suppress_symptoms` al doet voor de probe-kill (regel 429 en verder), waar een preciezere oorzaak een minder precieze verdringt. Sluit erop aan in plaats van er een tweede mechanisme naast te zetten.

Assertie: een test met een crash-event plus een bedienende pod voor hetzelfde component, die de aangepaste tekst verwacht, en een test zonder bedienende pod die de bestaande tekst onveranderd verwacht.

### Taak 5: de podlijst als endpoint, met de autorisatie op één plek

Bestand: `opi/api/logs_router.py`.

Voeg `GET /api/logs/pods/{project_name}` toe met queryparameters `deployment` en `component`, die de podlijst van taak 1 teruggeeft, beperkt tot dat ene component. Per pod: naam, ready, image (bronvorm), draaisinds, aantal herstarts, en of er een vorige poging te lezen is.

Dit endpoint is bewust ook de plek waar de vraag "mag deze gebruiker deze pod lezen" wordt beantwoord. Zet die controle in een functie die zowel dit endpoint als de WebSocket van taak 7 aanroept, zodat er niet twee antwoorden op dezelfde vraag ontstaan.

Assertie: een test dat een gebruiker zonder rechten op het project een 403 krijgt, en een test dat een onbekend component een lege lijst of een 404 geeft en niet de pods van een ander component.

### Taak 6: logs streamen van één pod, en van de vorige poging

Bestand: `opi/connectors/kubectl.py`.

Breid `stream_deployment_logs` uit met een optionele podnaam en een optionele vlag voor de vorige poging. Zonder podnaam blijft het commando exact wat het nu is, dus de label-selector, zodat het bestaande gedrag onveranderd blijft. Met podnaam wordt het `kubectl logs -f <pod> -c app -n <ns> --tail=<n>`.

Twee eigenschappen van `--previous` die het gedrag bepalen en die je moet respecteren:

1. Er valt niets te volgen aan een container die al gestopt is. De API levert het opgeslagen logboek en het proces eindigt meteen. De herverbindlus in `logs_websocket_router.py` (de `REATTACH_*`-constanten rond regel 470) mag in deze stand dus **niet** draaien, anders start hij elke paar seconden hetzelfde proces opnieuw en dumpt hij telkens dezelfde tekst.
2. Is er geen vorige poging, dan antwoordt de server met `Error from server (BadRequest): previous terminated container "app" in pod "<naam>" not found`. Dat is gemeten op productie op 28 augustus 2026. Die tekst hoort niet rauw in het paneel te belanden maar als leesbare melding, met een terugval op de gewone stroom.

Assertie: tests op de opbouw van het commando voor de drie standen, dus label-selector zonder podnaam, één pod, en één pod met de vorige poging. Er hoeft geen echte kubectl aan te pas te komen; `tests/test_logs_websocket_router.py` heeft hier al het patroon voor rond regel 425.

### Taak 7: de WebSocket laten kiezen, en die keuze afdwingen

Bestand: `opi/api/logs_websocket_router.py`.

Voeg aan `stream_logs` (regel 298) de optionele parameters voor podnaam en vorige-poging toe, en voeg dezelfde twee velden toe aan de bestaande `switch`-actie in `handle_client_messages` (rond regel 727), die de terminate-, leegmaak- en herstartvolgorde al goed doet.

De validatie is het belangrijkste deel van deze taak. Een podnaam uit de client mag nooit rechtstreeks in een kubectl-commando belanden. Toets hem met de functie uit taak 5 tegen de pods die bij dit project, deze deployment en dit component horen, en wijs af met een `error`-bericht wanneer hij daar niet in staat. Zonder die toets kan een lid met een geraden podnaam elke pod in het namespace tailen, en dat namespace draagt de deployments van andere teamleden. Toets ook bij `switch`, niet alleen bij het openen, en begrens de lengte zoals de bestaande componentnaamcontrole dat doet.

Het ad-hoc-podpad (de `LABEL_RUN`-tak rond regel 420, voor de database-console en jobs) krijgt geen podselectie en blijft ongewijzigd.

Assertie: een test die een podnaam meestuurt die niet bij het component hoort en aantoont dat er geen proces wordt gestart en dat de verbinding een `error` krijgt. Diezelfde test in de `switch`-variant. Plus een test dat een geldige podnaam wel doorkomt. Dit is de assertie waar deze taak op afgerekend wordt; de rest is bediening.

### Taak 8: de kiezer en de schakelaar in het paneel

Bestanden: `opi/templates_lotc/bg/_log-viewer.html.j2`, `static/js/log_viewer.js`, `opi/templates_lotc/bg/_argocd-deployment-card.html.j2`.

Zet naast de componentkiezer een podkiezer, in dezelfde `c-dropdown`-om-een-kale-`select`-vorm die de componentkiezer gebruikt. De kop van `_log-viewer.html.j2` legt uit waarom dat zo moet en waarom er na het vullen een `change` gestuurd wordt; dat geldt hier onverkort.

Opties: "Alle pods" als eerste, wat het gedrag van vandaag is, gevolgd door elke pod met een leesbaar label, bijvoorbeeld "...-9t87d, start niet, 5 herstarts" en "...-4qp6p, draait sinds 18 aug". De lijst komt van het endpoint uit taak 5, bij het openen en opnieuw bij elke componentwissel. Standaardkeuze: de niet-gereed pod als die er is, anders "Alle pods".

Daarnaast een schakelaar "vorige poging", in de vorm van de `c-switch-field` die de terugloopschakelaar al gebruikt. Hij is alleen bedienbaar wanneer er één pod gekozen is die een vorige poging heeft, en staat standaard aan wanneer de gekozen pod niet gereed is en herstarts heeft. Dat is precies het geval waarin de live stroom leeg blijft. Zet er in de statusregel bij dat dit een afgesloten logboek is en geen lopende stroom, anders lijkt een stilstaand paneel een kapotte verbinding.

Op de kaart krijgt elke podregel uit taak 3 een eigen "Logs"-knop die het paneel op díe pod opent. De bestaande knop "Logs bekijken" blijft staan en blijft op "Alle pods" openen. Let bij het doorgeven van argumenten op de `forceescape`-val die in de kop van dat sjabloon beschreven staat: `tojson` levert markup op en het gewone escapefilter slaat markup over, waardoor een aanhalingsteken het attribuut vroegtijdig sluit.

Assertie: uitbreiding van `tests/e2e/test_logviewer_gedrag.py`, dat al met een nagebootste WebSocket werkt. Toets dat de podkiezer gevuld wordt, dat een keuze een `switch`-bericht met de podnaam stuurt, dat de vorige-poging-schakelaar uit staat en niet bedienbaar is bij "Alle pods", en dat de statusregel bij een afgesloten logboek niet als storing leest.

### Taak 9: het feature-document

Bestand: `features/draaiende-pod-tonen.md`.

Beschrijf wat er getoond wordt, wanneer het verschijnt en waarom alleen dan, waarom `RollingUpdate` en `Recreate` een verschillend bericht krijgen, hoe de podselectie in de logstroom werkt, en waarom de vorige-poging-stand geen lopende stroom is. Neem de meting van `pr-114` op als het voorbeeld waar dit uit voortkomt.

## Wat groen moet blijven

`tests/test_deployment_diagnostics.py`, `tests/test_event_interpreter.py`, `tests/test_logs_websocket_router.py`, `tests/test_deployment_observation.py` en `tests/e2e/test_logviewer_gedrag.py` draaien nu allemaal groen en dekken het gedrag dat hier omheen staat. Wijzig ze alleen waar dit plan dat vraagt.

Sluit af met `uv run ruff check . --fix`, `uv run ruff format .` en `uv run pyright` vanuit `operations-manager/python`.

## Handmatige verificatie

Op de sandbox, want de productiesituatie is weg. Zet een component neer dat opkomt, laat het draaien, en rol daarna een image uit die meteen met exit 1 stopt. Dan ontstaat exact de stand van `pr-114`: een oude pod die bedient en een nieuwe die crasht. Controleer op de deploymentkaart dat de draairegel de oude pod met zijn image en datum noemt, dat de crashmelding zegt dat de vorige versie doordraait, dat de podkiezer beide pods aanbiedt, en dat de vorige-poging-stand de foutmelding van de gecrashte poging laat zien terwijl de live stroom leeg is.

Doe daarna dezelfde proef met een component met persistente opslag, dus in de `Recreate`-stand. Daar hoort geen draairegel te staan maar de rode melding dat er niets draait.
