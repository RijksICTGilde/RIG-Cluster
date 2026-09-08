# Eén pad voor formulier en API, en een controle die dat afdwingt

**Status**: plan, nog niets gebouwd
**Datum**: 2026-09-08
**Context**: `instructions/services.md` (de regel staat er sinds `2d5dd591` en `f66d815d` op drie plekken), `instructions/service-review-checklist.md` (waar de oordeelspunten horen), `features/service-config-api.md`

## Waarom

De regel dat het formulier en de API hetzelfde pad lopen stond in de instructies alleen als gevolg, ver van de stappenlijst, en is inmiddels als regel opgeschreven. Dat is niet genoeg. In deze codebase worden de regels nageleefd waar een test onder zit: een `ServiceType` zonder dienst, een schemafragment dat afdrijft, een dienst zonder helptekst. Voor "geen eigen endpoint, geen tweede validatie" faalt er niets, dus wijkt een bouwer er ongestraft van af en merken we het pas als de twee kanten uit elkaar zijn gelopen.

## Wat moet gelden

1. **Editables zijn het startpunt.** Het yaml-pad, de validators, de converters en de keuzelijsten horen daar, niet in een formulier en niet in een endpoint.
2. **Formulier en API lopen hetzelfde pad.** Scheiden mag waar de binnenkomende data echt verschilt, want een formulierpost is geen JSON-body, maar dan zo kort mogelijk: normaliseer naar dezelfde vorm en ga daarna door één keten.
3. **Validatie op één plek.** Bij voorkeur in de editables, anders in het configmodel of het schema. Nooit een controle die alleen het formulier draait en nooit een die alleen de API draait.
4. **Geen dubbele code.** Bestaat er al een mechanisme, gebruik dat. Een eigen oplossing naast een bestaande is de duurste vorm van "het werkte bij mij".
5. **Volledige API-documentatie.** Elk configveld heeft een `description`, elke operatie een `summary`, en de OpenAPI is wat een afnemer leest.
6. **De standaard API-route volgen.**

### Wat die standaard route precies is

Gemeten in `opi/api/v2/router.py`, `_register_service_config_routes` (regel 2894), die de registry afloopt en per `(dienst, laag)` routes genereert. Niets daarin noemt een dienstnaam.

```
PUT    /api/v2/projects/{project_name}/services/{service}/config/{target}[/{name}]
DELETE /api/v2/projects/{project_name}/services/{service}/config/{target}[/{name}]
PATCH  idem, voor de lijsten die een dienst als patchbaar declareert
GET    /api/v2/projects/{project_name}/services/{service}/config
GET    /api/v2/services  en  /api/v2/services/{service_name}
       /api/v2/projects/{project_name}/services/{service}/values/...   (owned_values_map)
       /api/v2/projects/{project_name}/services/{service}/<actie>      (api_actions)
```

Het achtervoegsel per laag komt uit `config_endpoint_path` in de catalogus, zodat de route die geregistreerd wordt en de route waar een foutmelding naar wijst dezelfde string zijn. De body is het configmodel van de dienst zelf. Eén tag per operatie (RC-45), anders staat een endpoint meerdere keren in Swagger.

## De controle

Twee soorten, en het verschil is belangrijk: wat mechanisch te meten is wordt een test, de rest wordt een oordeelspunt in de reviewchecklist. Een oordeelspunt als test verpakken levert een test op die iedereen omzeilt.

### Mechanisch, dus een test

Er is al een plek en een patroon: `tests/test_openapi_grouping.py` loopt `app.openapi()` af, `tests/test_service_providers.py` bewaakt de registry. Nieuw in diezelfde geest:

- **Herkomst van elke dienstroute.** Bouw de verwachte verzameling paden uit `SERVICES` maal `ConfigLayer` met dezelfde helpers die de generator gebruikt, en vergelijk met wat er in `app.routes` staat onder `/services/`. Faalt zodra iemand er met de hand een endpoint bij zet, en faalt ook als de generator een laag overslaat die de dienst wel declareert.
- **De body is het model van de dienst.** Voor elke gegenereerde schrijfroute: het body-model is `config_model_for(layer)` van die dienst, of de expliciet gedeclareerde enkelvoudige gevel. Geen los model dat toevallig lijkt op het echte.
- **Documentatie compleet.** Elke operatie heeft een `summary` en precies één tag; elk configveld heeft een `description`. Het tweede bestaat al (`test_service_config_field_descriptions.py`), het eerste hoort ernaast.
- **Geen dienstnaam in generieke code.** Bestaat als reviewpunt in de checklist, maar is prima te meten: de dienstnamen uit `ServiceType` mogen niet voorkomen in `opi/api/`, `opi/forms/` (buiten de afgeleide registers) en `opi/schemas/project_v2.json`.

### Oordeel, dus een reviewpunt

Toe te voegen aan `instructions/service-review-checklist.md`, met per punt wat "goed" is:

- **Geen dubbele code.** Loopt de validatie van dit veld één keer? Staat de omzetting van invoer naar opslag op één plek? Kan een lezer aanwijzen welke keten een waarde aflegt, van formulierveld en van API-body, en komen die twee snel samen?
- **Duidelijk en herbruikbaar pad.** Als het formulier en de API verschillen, is dat verschil dan benoemd en beperkt tot het binnenkomen, of loopt het door tot in de opslag?
- **Editables gebruikt.** Elk veld dat een gebruiker kan zetten heeft een editable met yaml-pad en validators. Een veld zonder editable moet uitgelegd worden, niet stilzwijgend bestaan.
- **Wizard flows gebruikt.** Een projectniveau-scherm loopt via de secties en flows die er zijn, niet via een eigen route of een eigen template.
- **Niets nieuws voor iets dat al bestaat.** Voor de vraag "waar hoort deze code" is de lagentabel in `instructions/services.md` (dienst, manager, connector) de leidraad, plus de bestaande haken. Vind je jezelf een dienstnaam in generieke code schrijven, dan ontbreekt er een haak en is die haak toevoegen goedkoper dan de uitzondering.

## Fasering

1. **De vier tests.** In `tests/`, in de geest van de bestaande openapi-tests. Verifieer: haal een gegenereerde route met de hand weg en de eerste test faalt; zet er met de hand een endpoint bij en hij faalt ook.
2. **De reviewpunten.** Een sectie in `instructions/service-review-checklist.md`, met dezelfde vorm als de rest: een check, hoe je hem doet, en wat goed is.
3. **De guardrail-tabel bijwerken** onderaan `instructions/services.md`, zodat de nieuwe tests daar staan met "faalt wanneer". Die tabel is wat een bouwer draait voordat hij klaar zegt.

## Wat we bewust niet doen

- Geen linter die "dubbele code" probeert te meten. Dat is een oordeel, en een slechte automaat eromheen kost meer dan hij oplevert.
- Geen tweede documentatiebestand. De regel staat in `instructions/services.md` en de controle in de checklist; een derde plek loopt uit de pas.
