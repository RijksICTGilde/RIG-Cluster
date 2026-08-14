# Unified service-config API

A registry-driven REST surface to configure any platform service, so a service's
config is reachable through the API and not only through the web wizard. It closes
the gap where the API could *select* services (by name) but only the UI could set
their config (keycloak template, storage mounts, health-check probes, and so on).

## Explicit, typed, per-service endpoints

Each configurable service gets its **own** endpoint whose request body **is** that
service's typed config model. The fields and enum values are therefore documented
explicitly per service in the OpenAPI spec (`/openapi.json`) -- so a client (CLI/UI)
can be generated from it -- instead of a generic config dict whose real shape is only
known at request time.

The routes are generated at startup from the service registry, so adding a service
to the registry adds its config endpoints automatically. Nothing hardcodes a service
name.

## Endpoints

All under `/api/v2`. The catalog list is project-independent and needs no API key;
the project-scoped calls require the project's `X-API-Key`.

| Method + path | Purpose |
|---|---|
| `GET /services` | List services with `config_schema_version`, the `targets` each accepts, and whether it is `configurable`. |
| `GET /projects/{p}/services/{service}/config` | Read the service's current config across every target it is set on. |
| `PUT /projects/{p}/services/{service}/config/project` | **Upsert** project-level config. Body = the service's config model. Async. |
| `PUT /projects/{p}/services/{service}/config/component/{component}` | Upsert component-level config. Async. |
| `PUT /projects/{p}/services/{service}/config/deployment/{deployment}` | Upsert deployment-level config. Async. |
| `DELETE /projects/{p}/services/{service}/config/{target}[/{name}]` | Clear the config at a target (keeps the service selected). Async. |
| `PATCH /projects/{p}/services/{service}/config/{target}[/{name}]` | Add or remove single entries in a config that IS a list. Async. |
| `PATCH /projects/{p}/services/{service}/config/{target}[/{name}]/{list}` | Add or remove single entries in a named list INSIDE a config object. Async. |

The write routes exist only for the (service, target) pairs a service actually
supports -- e.g. `keycloak/config/project` exists, `keycloak/config/component/...`
does not (404). `deployment-component` is intentionally not generated: no service
accepts config there today (per-mount storage clone state is set via the image-update
endpoint's actions, which is an operation, not config).

## Een lijst bijwerken zonder de rest over te typen

Een PUT schrijft het hele blok. Bij een lijst betekent dat: één regel erbij zetten is
alle andere regels opnieuw meesturen, en wie dat niet weet wist ze. Dat is echt gebeurd
-- een project raakte zijn invites kwijt, en omdat de invitesleutel bewust in geen enkel
leesantwoord staat (het is het geheim in de link) was de eerste invite daarna niet eens
meer te reconstrueren. Een tweede invite kostte dus de eerste.

Daarom heeft elke lijst een PATCH met `{add, remove}`, in twee smaken die dezelfde body
hebben:

- **De config IS een lijst** (`persistent-storage`, `temp-storage`, `attachments` op
  componentniveau). De PATCH staat op het configpad zelf.
- **De config BEVAT een lijst** (`invite.active`, `cross-domain-access.inbound` en
  `.outbound`, `sleep-mode.match`). De PATCH staat op het configpad plus de naam van de
  lijst. Eén route per lijst, want de twee richtingen van cross-domain-access bevatten
  verschillende regels en één body kan niet voor allebei getypeerd zijn. Alles wat naast
  de lijst in het blok staat blijft ongemoeid.

Regels, in beide smaken gelijk: `add` neemt hele entries, `remove` neemt sleutels.
Verwijderen gaat eerst, dus een sleutel die in beide lijsten staat wordt vervangen. Een
sleutel die er niet is, is een no-op. Het taakresultaat meldt `added`, `updated` en
`removed`.

De sleutel komt uit het configmodel zelf (`opi/services/config_lists.py`): `ITEM_KEY` op
een `RootModel`-lijst, `ITEM_KEYS` op een model met lijsten erin. Een lijst met platte
waarden (`sleep-mode.match` bevat globpatronen) heeft geen sleutelveld en mapt naar
`None`: de waarde IS zijn identiteit, dus `add` is een vereniging en `remove` neemt
waarden. Zo'n lijst wordt gecontroleerd door het model dat hem bezit -- de
patrooncontrole van sleep-mode -- want er is geen entrymodel om tegenaan te valideren.

```bash
# een tweede invite erbij, zonder de eerste aan te raken
curl -X PATCH https://.../api/v2/projects/algor-odc/services/invite/config/project/active \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"add": [{"key": "tweede-geheim", "realm-roles": ["editor"]}]}'

# één toegangsregel weg, de andere richting blijft staan
curl -X PATCH https://.../api/v2/projects/algor-odc/services/cross-domain-access/config/project/outbound \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"remove": ["naar-api"]}'

# een patroon erbij; de overige sleep-mode-instellingen blijven zoals ze staan
curl -X PATCH https://.../api/v2/projects/algor-odc/services/sleep-mode/config/project/match \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"add": ["test-*"]}'
```

Wat dit NIET oplost: verwijderen gaat op de sleutel, en bij `invite.active` is die
sleutel het geheim dat niet terug te lezen is. Wie een invite zelf aanmaakte kent hem en
kan hem weghalen; een invite die iemand anders (of de portal) aanmaakte is via de API
niet te verwijderen zonder die sleutel. Toevoegen -- het punt waar het misging -- kan nu
wel zonder.

### Example

```bash
# keycloak (project-level). The body IS KeycloakConfig -- see its fields in the spec.
curl -X PUT https://.../api/v2/projects/algor-odc/services/keycloak/config/project \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"template": "algoritmeregister", "additional_redirect_uris": ["http://localhost:8080/*"]}'
# -> 202 Accepted, {task_id, poll_url}; poll /api/tasks/{task_id}

# health-check on a named component. scheme is a Literal -> the spec lists the enum.
curl -X PUT https://.../api/v2/projects/algor-odc/services/health-check/config/component/backend \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"scheme": "http", "port": 8080, "liveness-path": "/healthz"}'

# read everything
curl -H "X-API-Key: $KEY" https://.../api/v2/projects/algor-odc/services/keycloak/config

# clear
curl -X DELETE -H "X-API-Key: $KEY" \
  https://.../api/v2/projects/algor-odc/services/keycloak/config/project
```

## Toegestane waarden per veld

Het schema zegt welke velden er zijn; `x-choices` en `x-choices-source` zeggen welke waarden erin mogen. Ze worden bij het opbouwen van `/openapi.json` afgeleid uit dezelfde `values_provider` waarmee het formulier in de portal zijn keuzelijst vult (`opi/api/openapi_choices.py`), dus er is geen tweede lijst die kan gaan afwijken.

- **`x-choices`** staat op een veld met een vaste lijst en bevat per waarde `const` (de waarde), `title` (het label) en soms `description`. Kent het configmodel de keuze zelf, dan staat er ook een gewone `enum` en zijn dat dezelfde waarden; `x-choices` voegt dan alleen het label toe. Staat er geen `enum`, dan accepteert het model een breder formaat en is dit de lijst die de portal aanbiedt: `sleep-after-deploy` neemt elke duur (`90m` mag), maar biedt `4h` tot en met `168h` aan.
- **`x-choices-source`** staat op een veld waarvan de waarden uit het project komen, met `description` en, als er een endpoint voor is, `endpoint` en `path`. Een opsomming zou daar een momentopname van een willekeurig project zijn. Voorbeeld: `waker-component` van sleep-mode wijst naar `GET /api/v2/projects/{project_name}/components`, `components[].name`.
- Booleans krijgen geen keuzelijst: het formulier toont Ja/Nee, maar de API wil een echte JSON-boolean, en dat zegt het type al.
- Een standaardwaarde staat gewoon in `default`.

Elke provider die een service-configveld vult declareert zelf welke van de twee het is (`options_source` op de provider, `None` als de lijst vastligt). Zonder die declaratie komt er geen keuzelijst in de documentatie, want een projectafhankelijke lijst zonder project zou de paar overgebleven opties tonen alsof dat de toegestane waarden zijn.

## How it works

The typed body is validated by FastAPI at request time (an unknown or out-of-enum
value returns 422 immediately). The write then goes through the async task system
(`TaskType.CONFIGURE_SERVICE` -> `handle_configure_service` ->
`ProjectManager.configure_service` / `clear_service_config`), exactly like the other
v2 mutations. The mutation itself is the pure `ServiceAdapter.set_service_config` /
`remove_service_config`: it writes the same `{name, config}` (project) /
`{reference, config}` (component/deployment) records the wizard writes, finds an
existing entry with `service_entry_name` and promotes a bare-string selection in
place rather than appending a duplicate. Only the fields the caller actually sent are
written (`model_dump(exclude_unset=True)`), so an unset optional field leaves no key.

Configuring a service on a **component or deployment implicitly selects it at the
project level** (a bare-string entry in the root `services` list), so the caller does
not have to add it to the root list first -- a component service must resolve to a
project-level service (a structural check). No explicit project-level config is
assumed: if the service genuinely requires project-level config, the bare selection
fails validation there, which is a clear signal rather than a silent gap. An existing
project entry (bare or configured) is never duplicated or demoted. A bare selection
carries no config, so it does not appear in the read (`GET .../config`).

The save chokepoint (`save_and_commit_project` -> `validate_service_configs`) is the
backstop: it re-validates the block against the service's typed model, so a config
that slips past the request-time check still fails the task with `validation_error`
and the accepted-field list. No schema-version bump and no change to the global
project schema is involved: the record shapes are already valid there. After a
successful write the project is processed (reconciled) so the config takes effect. A
`DELETE` that changed nothing is a quiet, idempotent no-op.

## Which services are configurable, and where

Measured from the registry (not assumed):

| Target | Services |
|---|---|
| `project` | authorization-wall, keycloak, namespace-postgresql-database, redis, sleep-mode, minio-storage |
| `component` | attachments, health-check, metrics-scraper, persistent-storage, temp-storage, publish-on-web |
| `deployment` | minio-storage |

`namespace-redis` and `platform` carry no config by design and expose no target.
`postgresql-database` has a config model but declares it on no layer, so it is not
configurable through this endpoint (a known gap, tracked in
`docs/service-review-2026-08.md`).

## Relationship to the older endpoints

`POST /api/v2/projects/{p}/services` and its v1 twin (add-a-service-by-name) are
marked **deprecated**: the typed config endpoints select a service and set its config
in one call. The component `services: list[str]` fields on the add/update-component
endpoints stay -- they are bare-name *selection*, which this config surface does not
replace -- but per-service config now belongs here. The image-update endpoint's
per-mount storage *actions* (clone/recreate) are storage operations, not config, and
are out of scope.

## Dependencies

- Service registry and the `Service` config hooks (`opi/services/catalog/base.py`).
- Async task system (`opi/core/async_task_service.py`, `opi/worker_main.py`).
- The save chokepoint `ProjectManager.save_and_commit_project` ->
  `validate_service_configs`.

## Tests

- `tests/test_service_config_api.py` -- the pure core, the round-trip through the
  validation chokepoint, the endpoint helpers, and a measured API-config coverage
  guard over every service.
- `tests/test_v2_flow.py::TestConfigureServiceFlow` -- the HTTP surface: the catalog
  list, the typed-body upsert/clear task payloads, the OpenAPI per-service schema,
  auth, and the 404/422 gates.
- `tests/test_openapi_config_choices.py` -- de toegestane waarden in `/openapi.json`,
  met sleep-mode als concreet geval, plus de drift-lock tussen keuzelijst en `enum` en
  de eis dat elke provider zijn bron declareert.
