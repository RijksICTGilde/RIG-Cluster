# De lagen van de toetsen, en hoe je kiest

De suite is groot en groen, en dat zegt minder dan het lijkt: het grootste deel meet onze
eigen code tegen mocks. Toen Keycloak 26.8.1 een dubbele config-alias ging weigeren bleef
alles groen, en een gebruiker meldde de fout op productie. Een mock die vertelt wat wij
denken dat de afhankelijkheid doet, meet precies het verkeerde.

Daarom zes lagen. Kies de laagste laag die het gedrag echt meet.

| Laag | Wat hij meet | Waar hij staat | Hoe je hem draait |
|---|---|---|---|
| unit | onze eigen logica: een beslissing, een naam, een vorm | `tests/test_*.py` | `uv run pytest tests/test_x.py`, voor een bestand uit de brede laag met `-m wide` erbij |
| live per afhankelijkheid | het contract met de ECHTE afhankelijkheid in de versie die productie draait | `tests/integration/test_*_live.py` | `task test-keycloak`, `task test-postgres`, `task test-minio`, `task test-forgejo`, `task test-kubernetes`, `task test-argocd` |
| scenario | de ORDE van meerdere acties, met een invariant na elke stap | `tests/integration/test_*_scenarios.py` | dezelfde task als de groep |
| het opstarten | de hele opstartvolgorde over ALLE afhankelijkheden tegelijk, in drie rondes | `tests/integration/test_startup_live.py` | `task test-startup` |
| de taken | elk `TaskType` van begin tot eind, via de echte taakweg, met een invariant per taak | `tests/integration/test_tasks_live.py` | `task test-taken` |
| e2e | het portaal in een browser, en de sandbox | `tests/e2e/` | `uv run pytest tests/e2e -m "e2e and not sandbox"`, `task test-e2e-sandbox` |

De standaardrun (`uv run pytest`, `task test`) draait de snelle helft van de unit-laag en
de statische toetsen; de brede helft en de live groepen slaat hij over (zie de volgende
paragraaf). Van de live groepen geldt: **een groene standaardrun zegt niets over de live
groepen.** In CI draait elke groep in zijn eigen job, en daar is "overgeslagen" een FOUT en
geen skip.

## Wanneer welke laag draait

De unit-laag is te groot geworden om altijd in zijn geheel te draaien: ruim 19.000 toetsen
in meer dan 1000 bestanden, en een volledige run kost een half uur. Daarom is hij in twee
gesplitst, met de marker `wide`.

| Laag | Wat erin zit | Hoe je hem draait | Wanneer |
|---|---|---|---|
| **snel** | eigen logica, statische toetsen, renderen, HTTP-client zonder echte afhankelijkheid: 15.800 toetsen, **3 minuten 23** (gemeten 7 november 2026) | `task test` | lokaal tijdens het werk, en in elke gate |
| **breed** (`wide`) | toetsen met echte database, en bestanden >2 seconden: 2978 toetsen in 10 minuten 28, plus de databasehelft die een Postgres in Docker vraagt | `task test-wide` | in CI op elke PR, en eenmaal vóór de merge |
| **live groepen** | het contract met de echte afhankelijkheid | `task test-live-groepen` | op hun padfilters in CI, en als handstap vóór de merge |
| **e2e** | het portaal in een browser, en de sandbox | `task test-e2e`, `task test-e2e-sandbox` | vóór een release, en waar een taak het portaal raakt |

`task test-all` draait de snelle en de brede laag achter elkaar. In CI draaien ze samen, in
vier shards (`.github/workflows/ci.yml`): het padfilter en de coverage-drempels gelden over
beide lagen, dus een toets die naar de brede laag verhuist verdwijnt niet uit de dekking.

De marker zet je niet met de hand: `tests/conftest.py` markeert als `wide` elke toets die
`orm_db`, `migration_db` of `_orm_db_url` vraagt, plus elk bestand in `BREDE_BESTANDEN`.
Die lijst is een MEETWAARDE: het zijn de bestanden die meer dan twee seconden kosten (een
echte `sops`, `age`, `kustomize` of `git`, een scan over de hele boom, of simpelweg veel
werk). Opnieuw maken na een meting:

```bash
task test-inventaris
uv run python scripts/suite_inventory.py /tmp/zad-test-inventaris.xml --above 2
```

Een bestand dat om een ANDERE reden dan zijn duur in de brede laag hoort, zet zelf
`pytestmark = pytest.mark.wide` bovenaan, met de reden erbij.

**Tijdens het werk draai je niet de hele laag.** `task test-changed` kiest de toetsbestanden
die bij je gewijzigde bestanden horen, op naam: een gewijzigde `opi/x/y.py` levert de
toetsen die `opi.x.y` noemen. Dat is niet volledig bedoeld, want CI draait daarna de hele
laag. Hij draait wel ALLE toetsen in de bestanden die hij kiest, ook de brede: een selectie
zonder de brede helft meldt groen zonder te draaien wat ze noemt. Zit er een bestand met
een echte database bij, dan heb je daar dus een Postgres in Docker voor nodig.

## Wat er niet in de suite hoort

De suite groeit harder dan wat hij meet. Deze vier soorten gaan eruit zodra je ze
tegenkomt, en komen er niet in:

- **Een constante tegen zichzelf.** Een toets die van een declaratie attribuut voor
  attribuut herhaalt wat er staat (`assert EDITABLE.yaml_path == "deployments[*]/..."`)
  faalt alleen als iemand de declaratie met opzet wijzigt, en zegt niets over gedrag. Meet
  wat de declaratie DOET: wat het formulier rendert, of wat er in het projectbestand komt.
  Een waarde die een afspraak met iets buiten ons is (een DNS-record, een veldnaam uit een
  schema van het cluster) is géén constante tegen zichzelf: die hoort vast te liggen.
- **Alleen een mock-aanroep**, terwijl een live groep of een scenario dat gedrag nu echt
  meet. De indeling van de mock-zwaarste bestanden staat in de audit van RC-434 (PR #417):
  wat de ORDE van een taak met mocks naspeelt, wordt door een scenario vervangen; wat een
  vaste tekst of een beslissing meet, blijft.
- **Dubbel.** Hetzelfde gedrag in een ticketbestand naast het modulebestand. Voeg samen in
  het bestand dat bij de module hoort.
- **Een achtergebleven script.** Een bestand in `tests/` dat `print` in plaats van `assert`
  doet, of dat een port-forward naar een omgeving van jaren terug nodig heeft. Het kan niet
  falen, dus het meet niets.

Een module die dekking verliest doordat er een toets weg is, is een teken dat die toets wél
iets mat: kijk dan opnieuw. De drempels in `.github/workflows/ci.yml` (71, en 86 voor de
beveiligingskritische modules) gaan niet omlaag.

## De inventaris

`task test-inventaris` draait de unit-suite eenmaal met een junit-xml en maakt daar het
overzicht van: per bestand het aantal toetsen, de duur, de soort en de module die het
raakt, plus de verdeling van tijd en aantal over de soorten en de traagste bestanden. De
soort komt uit wat het bestand gebruikt (`scripts/suite_inventory.py`), niet uit zijn naam.

Het overzicht hoort op de PR en niet in de repo: het veroudert bij elke wijziging. Draai de
task als je wilt weten waar de tijd heen gaat, niet per taak.

## Drie regels

**Toets de uitkomst, niet de aanroep.** `assert_called_once_with` toetst dat wij onze eigen
mock aanroepen zoals wij dachten; het zegt niets over wat er gebeurde. Gebruik hem alleen
als de aanroep zelf het gedrag is, bijvoorbeeld "er wordt niets naar productie gestuurd" of
"de connector krijgt het wachtwoord nooit in de argv". In de live groepen lees je na elke
schrijfactie terug wat de afhankelijkheid werkelijk opsloeg, met een eigen kanaal
(`AdminApi` voor Keycloak, een eigen `asyncpg`-verbinding voor PostgreSQL, `mc` voor MinIO,
de API voor Forgejo) en niet met de connector onder toets.

**Een mock alleen op de grens met een afhankelijkheid die een live groep heeft.** Heeft een
afhankelijkheid geen live groep (SeaweedFS, metrics en DNS vandaag; van mail is alleen gemeten
wat het opstarten gebruikt), dan mag je hem mocken, maar dan is dat pad `planned` op de kaart
en niet gemeten. Mock nooit onze eigen code weg om een toets te laten slagen.

**Elke actie staat op de kaart.** `operations-manager/python/tests/regression/actions.yaml`
noemt elke stap van het opstarten en elk `TaskType`, met per afhankelijkheid `measured` (met
de toets), `planned` (met de fase) of `exempt` (met de reden).
`tests/test_actions_map.py` faalt zodra er een stap of taak bij komt die er niet op staat.
Vul naar waarheid: wat alleen met mocks gemeten wordt, is `planned`.

## Een nieuwe actie, en waar zijn toets komt

1. **Een nieuwe methode op een connector** krijgt zijn toets in de live groep van die
   afhankelijkheid: aanroepen, en terugleren wat de afhankelijkheid opsloeg.
   `tests/test_live_group_coverage.py` faalt in de standaardrun zodra `opi/` een methode
   aanroept die de groep niet aanroept. Meet je hem met opzet niet, zet hem dan in de
   `exempt` van die afhankelijkheid, met de reden.
2. **Een nieuwe STAP of een nieuwe ORDE in een manager of handler** krijgt zijn toets in de
   scenariomodule van die afhankelijkheid, over meer dan een deployment, met de invariant
   die na elke stap de hele toestand terugleest. Daar zat de 409 van Keycloak 26.8.1
   (RC-433), en daar zit het antwoord op "kan deployment A bij de database van B".
3. **Een nieuwe stap in het opstarten of een nieuw `TaskType`** krijgt een regel op de kaart.
4. **Een nieuwe container in een testopstelling** leest zijn image uit de pin in
   `infrastructure/` of `bootstrap/` (`tests/image_pins.py`), nooit uit een eigen tag.
   `tests/test_test_images_follow_pins.py` bewaakt dat: zo kan een opstelling niet stil
   afdrijven van wat er uitgerold is, zoals de authwall-opstelling deed (Keycloak 25.0.6
   tegen een productie op 26.8.1).

## Wat een live groep moet doen

Volg `keycloak_live`; dat is het voorbeeld, en de rest is eraan gelijk gemaakt.

- **Een marker per afhankelijkheid** (`keycloak_live`, `postgres_live`, `minio_live`,
  `forgejo_live`, `kubernetes_live`, `argocd_live`, plus `startup_live` voor het opstarten en
  `tasks_live` voor de taken, beide over alle zeven), plus `requires_infra`, zodat de
  standaardrun ze overslaat.
- **De image komt uit de pin**, gelezen door de task. `IMAGE=` voor een andere tag.
  Niet elke image start zichzelf: de PostgreSQL van het platform is een CNPG-image zonder
  ENTRYPOINT, die de operator opstart. Elke plek die hem los start geeft daarom
  `scripts/postgres-standalone-start.sh` mee.
- **Zonder de omgeving slaat de groep over**; met de vereist-variabele van de groep
  (`POSTGRES_LIVE_REQUIRED` en zijn broers) is ontbreken een FOUT. CI zet die altijd, want
  daar betekent "overgeslagen" dat de job groen is zonder meting.
- **De groep ruimt op wat hij maakt**, ook bij falen, en weigert tegen iets anders dan
  localhost te draaien (`LiveGroup.assert_local`). Alles wat hij maakt begint met het
  voorvoegsel van die groep (`zadlive`, `zadstart`, `zadta`), zodat een mens ziet wat van de
  toetsen is en twee groepen elkaars opruiming niet meenemen.
- **Een job in `.github/workflows/integration-tests.yaml`**, op de paden van die connector,
  zijn manager, zijn handlers, zijn pin en de groep zelf. Geen geplande run erbij: een groep
  meet een gepinde image, dus hij veroudert niet stil zolang het padfilter de pin omvat.
  Lokaal draait `task test-live-groepen` ze alle acht achter elkaar.

De gedeelde hulp staat in `tests/integration/live.py` (de omgeving lezen, de
localhost-grendel) en per afhankelijkheid in `tests/integration/<naam>_live.py` (de
opstelling en het terugleeskanaal). De opstartroute van een groep staat in de Taskfile, met
`_wacht-op` voor het wachten. Schrijf geen tweede exemplaar ernaast.

## Het wegwerpcluster van kubernetes_live, argocd_live, startup_live en tasks_live

Vier groepen hebben een cluster nodig in plaats van een container, en ze delen er een:

```bash
task test-cluster-up      # kind + ArgoCD + Forgejo, met de pins van de sandbox
task test-kubernetes      # de groep kubernetes_live
task test-argocd          # de groep argocd_live
task test-startup         # de groep startup_live
task test-taken           # de groep tasks_live
task test-cluster-down    # cluster weg, kubectl-context terug
```

Wat erin zit, en waarom:

- **kind met de node-image uit `bootstrap/sandbox/kind-config.yaml`.** Die pin hoort de
  Kubernetes-versie van ODCN te volgen; die stond bij het schrijven niet vast, dus hij
  volgt de sandbox. Verandert dat, dan is het een regel in dat bestand:
  `tests/image_pins.py` leest hem, en `tests/test_test_images_follow_pins.py` bewaakt dat
  de meetclusters (ook die van `netpol:meet`) hem gebruiken.
- **De ArgoCD van het platform**, uit de CR van de sandbox-overlay, met de CMP-sidecar uit
  `bootstrap/rig-system/kustomize/components/sops-plugin`. Die sidecar hoort erbij: het
  Application-sjabloon van OPI noemt de plugin `kustomize-sops`, dus zonder hem rendert
  ArgoCD geen enkele app van een tenant.
- **Een Forgejo op sqlite**, zoals de groep `forgejo_live` er een start. De
  PostgreSQL-route van de sandbox vraagt CNPG en de ontwikkelaarssleutel voor de
  SOPS-secrets, en dit cluster leeft een paar minuten.
- **Eigen naam en eigen poorten**, dus hij draait naast `rig-sandbox`. kind zet je
  kubectl-context op dit cluster; `task test-cluster-down` zet hem terug.

De vier groepen die erop meten (`kubernetes_live`, `argocd_live`, `startup_live` en
`tasks_live`) draaien met een eigen KUBECONFIG en weigeren tegen iets anders dan een
`kind-`context te meten. Elke groep heeft zijn EIGEN voorvoegsel (`zadlive`, `zadstart`,
`zadta`), want de opruiming gaat op dat voorvoegsel en zou anders de namespaces van een
andere groep weghalen.

`task test-startup` en `task test-taken` delen hun hele opstelling: die staat als een eigen
interne taak in de Taskfile (`_live-opstelling`), en de aanroeper zegt alleen welke toetsen
eroverheen gaan. Twee kopieen zouden betekenen dat de groepen uit elkaar lopen zodra er een
adres bij komt.

Wat alleen op ODCN bestaat (de webhook van Capsule, de VPA-operator) staat met een reden op de
kaart: die operators hebben in deze repo geen gepinde versie.

## De groep startup_live: het hele opstarten

De andere groepen meten een afhankelijkheid per keer. Het opstarten van OPI is meer dan de som
daarvan: een vaste volgorde over alle afhankelijkheden tegelijk, waarvan elke stap moet slagen
op een leeg cluster EN op een cluster dat al draait.

```bash
task test-cluster-up      # hetzelfde wegwerpcluster als kubernetes_live en argocd_live
task test-startup         # de groep startup_live
task test-cluster-down
```

`task test-startup` zet de opstelling neer die de andere groepen al hadden en voegt er een toe:
PostgreSQL, Keycloak, MinIO en de mailrelay als container naast de toets (zoals fase 1), en
Kubernetes, ArgoCD en Forgejo op het wegwerpcluster (zoals fase 2). De Keycloak start via
dezelfde interne taak als `task test-keycloak` (`_keycloak-container`), zodat er geen tweede
opstart naast staat die stil kan afwijken van wat productie start.

Wat de groep doet:

- **OPI draait in het proces van de toets**, met de echte `lifespan` en instellingen die naar
  de opstelling wijzen. Niet als uitgerolde pod: de uitrol-manifests meten de kustomize-toetsen,
  en wat hier telt is wat de CODE bij het opstarten doet.
- **Drie rondes**: een leeg cluster, direct nog een keer in hetzelfde proces, en na een
  herstart met de toestand van ronde 1 er al. Na elke ronde dezelfde invariant, en na ronde 2
  en 3 bovendien dat er niets dubbel is aangemaakt en niets is weggehaald.
- **Een storing bij het opstarten**: PostgreSQL en de mailrelay zijn weg als OPI start en komen
  daarna terug. Dat is het gedrag dat op productie telt, want een verse uitrol komt op voordat
  zijn afhankelijkheden antwoorden.
- **De achtergrondtaken** die `lifespan` daarna start (TaskWorker, schedulers, watchers) moeten
  zonder fout starten en bij het afsluiten weer netjes stoppen. Ze staan daarom in deze groep
  AAN, en de toets leest `app.state`: een scheduler die niet start wordt gelogd en niet
  geworpen, dus de log is geen poort.

Wat met opzet buiten de groep blijft, staat met een reden op `actions.yaml`: de DNS-stappen
(TransIP is een externe dienst zonder image), de metrics-probe (die mag zijn bron missen, dat is
bedoeld gedrag), `_resolve_running_image` (leest de eigen pod, en die is er niet) en de
federatie (die vraagt een tweede cluster).

## De groep tasks_live: elke taak van begin tot eind

Alles wat ZAD voor een project doet, loopt via een taak. `tasks_live` draait ze alle
vijfentwintig langs de ECHTE taakweg: aangemaakt zoals het portaal of de zad-cli dat doet,
uitgevoerd door de echte `TaskWorker`, en na elke taak een invariant die de toestand
terugleest uit de afhankelijkheid zelf.

```bash
task test-cluster-up      # hetzelfde wegwerpcluster
task test-taken           # de groep tasks_live
task test-cluster-down
```

- **Dezelfde opstelling als `startup_live`** (zie hierboven), met kopia erbij uit de pin van
  de OPI-image: de backup- en restore-scenario's draaien de binary die in productie draait,
  met de bestemming in de wegwerp-MinIO.
- **OPI draait als echte HTTP-server in de event loop van de toets.** Een echte server en
  geen ASGI-transport, zodat de helpers van de sandboxsuite (`tests/e2e/helpers/sandbox_api.py`)
  er onveranderd op kunnen; dezelfde loop, zodat de worker en de pools er maar een keer zijn.
  Een blokkerende aanroep uit de toets gaat daarom via `asyncio.to_thread`.
- **Een keten op EEN project**, in de orde waarin een project leeft: aanmaken, een component,
  twee deployments, elke dienst, de configuratie, de bijlagen, de klonen, slapen en wekken,
  een backup en een restore, de rotaties, en alles weer weg. De laatste invariant is dat er na
  het verwijderen niets meer van het project in een van de zeven afhankelijkheden staat.
- **De autorisaties zijn de echte.** De aanmaak over de API vraagt een SSO-token, en dat
  haalt de groep met authorization code plus PKCE uit de Keycloak van de opstelling. De taken
  die alleen een webroute hebben (verwijderen, slapen en wekken) gaan met een sessie en een
  CSRF-token, zoals het portaal.

Wat met opzet buiten de groep blijft, staat met een reden op `actions.yaml`: SeaweedFS en
Redis (de opstelling heeft er geen server voor) en de chisel-tunnel van de kloontaken (die
vraagt een tweede omgeving).

## Achtergrond

`features/keycloak-eigen-image.md` beschrijft wat de Keycloak-groep allemaal meet en waarom
de invariant na elke stap eist dat het hele realm nog klopt. De fasen voor de
afhankelijkheden die nog `planned` zijn, staan boven in `actions.yaml`.
