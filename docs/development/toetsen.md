# De lagen van de toetsen, en hoe je kiest

De suite is groot en groen, en dat zegt minder dan het lijkt: het grootste deel meet onze
eigen code tegen mocks. Toen Keycloak 26.8.1 een dubbele config-alias ging weigeren bleef
alles groen, en een gebruiker meldde de fout op productie. Een mock die vertelt wat wij
denken dat de afhankelijkheid doet, meet precies het verkeerde.

Daarom vier lagen. Kies de laagste laag die het gedrag echt meet.

| Laag | Wat hij meet | Waar hij staat | Hoe je hem draait |
|---|---|---|---|
| unit | onze eigen logica: een beslissing, een naam, een vorm | `tests/test_*.py` | `uv run pytest tests/test_x.py` |
| live per afhankelijkheid | het contract met de ECHTE afhankelijkheid in de versie die productie draait | `tests/integration/test_*_live.py` | `task test-keycloak`, `task test-postgres`, `task test-minio`, `task test-forgejo` |
| scenario | de ORDE van meerdere acties, met een invariant na elke stap | `tests/integration/test_*_scenarios.py` | dezelfde task als de groep |
| e2e | het portaal in een browser, en de sandbox | `tests/e2e/` | `uv run pytest tests/e2e -m "e2e and not sandbox"`, `task test-e2e-sandbox` |

De standaardrun (`uv run pytest`) draait de unit-laag en de statische toetsen. De live
groepen slaat hij over: **een groene standaardrun zegt niets over de live groepen.** In CI
draait elke groep in zijn eigen job, en daar is "overgeslagen" een FOUT en geen skip.

## Drie regels

**Toets de uitkomst, niet de aanroep.** `assert_called_once_with` toetst dat wij onze eigen
mock aanroepen zoals wij dachten; het zegt niets over wat er gebeurde. Gebruik hem alleen
als de aanroep zelf het gedrag is, bijvoorbeeld "er wordt niets naar productie gestuurd" of
"de connector krijgt het wachtwoord nooit in de argv". In de live groepen lees je na elke
schrijfactie terug wat de afhankelijkheid werkelijk opsloeg, met een eigen kanaal
(`AdminApi` voor Keycloak, een eigen `asyncpg`-verbinding voor PostgreSQL, `mc` voor MinIO,
de API voor Forgejo) en niet met de connector onder toets.

**Een mock alleen op de grens met een afhankelijkheid die een live groep heeft.** Heeft een
afhankelijkheid geen live groep (Kubernetes, ArgoCD vandaag), dan mag je hem mocken, maar
dan is dat pad `planned` op de kaart en niet gemeten. Mock nooit onze eigen code weg om een
toets te laten slagen.

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
  `forgejo_live`), plus `requires_infra`, zodat de standaardrun ze overslaat.
- **De image komt uit de pin**, gelezen door de task. `IMAGE=` voor een andere tag.
  Niet elke image start zichzelf: de PostgreSQL van het platform is een CNPG-image zonder
  ENTRYPOINT, die de operator opstart. Elke plek die hem los start geeft daarom
  `scripts/postgres-standalone-start.sh` mee.
- **Zonder de omgeving slaat de groep over**; met de vereist-variabele van de groep
  (`POSTGRES_LIVE_REQUIRED` en zijn broers) is ontbreken een FOUT. CI zet die altijd, want
  daar betekent "overgeslagen" dat de job groen is zonder meting.
- **De groep ruimt op wat hij maakt**, ook bij falen, en weigert tegen iets anders dan
  localhost te draaien (`LiveGroup.assert_local`). Alles wat hij maakt begint met
  `zadlive`, zodat een mens ziet wat van de toetsen is.
- **Een job in `.github/workflows/integration-tests.yaml`**, op de paden van die connector,
  zijn manager, zijn handlers, zijn pin en de groep zelf. Geen geplande run erbij: een groep
  meet een gepinde image, dus hij veroudert niet stil zolang het padfilter de pin omvat.
  Lokaal draait `task test-live-groepen` ze alle vier achter elkaar.

De gedeelde hulp staat in `tests/integration/live.py` (de omgeving lezen, de
localhost-grendel) en per afhankelijkheid in `tests/integration/<naam>_live.py` (de
opstelling en het terugleeskanaal). De opstartroute van een groep staat in de Taskfile, met
`_wacht-op` voor het wachten. Schrijf geen tweede exemplaar ernaast.

## Achtergrond

`features/keycloak-eigen-image.md` beschrijft wat de Keycloak-groep allemaal meet en waarom
de invariant na elke stap eist dat het hele realm nog klopt. De fasen voor de
afhankelijkheden die nog `planned` zijn, staan boven in `actions.yaml`.
