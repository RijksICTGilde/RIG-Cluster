# Env-vars en aliassen via de API

De omgevingsvariabelen en aliassen van een component zijn nu ook via de REST-API te
beheren, per stuk, in plaats van alleen via de wizard en de bewerkmodals.

## Waarom dit er niet al was

`user-env-vars` en `aliases` waren de enige twee geregistreerde services zonder ook maar
één endpoint. Dat was geen vergissing. De generieke config-routes
(`PUT/DELETE /projects/{p}/services/{svc}/config/...`) schrijven het config-blok in een
`services:`-lijst, en deze twee services schrijven juist een **eigen property op het
component** (`user-env-vars:`, `aliases:`). De generieke machinerie zou dus het verkeerde
ding adresseren, en `_accepts_config_at` in `opi/api/v2/router.py` sluit ze daarom
bewust uit.

Wat ontbrak was een endpoint voor de owned-property-vorm. Dat is wat hier staat.

## De endpoints

Twee padvormen, vijf operaties per vorm:

```
componentniveau            /api/v2/projects/{project}/services/{svc}/values/component/{component}
deployment/componentniveau /api/v2/projects/{project}/services/{svc}/values/deployment/{deployment}/component/{component}
```

| operatie | methode + pad | body |
|---|---|---|
| toevoegen (1..n) | `POST .../values/...` | `{"values": {"NAAM": "waarde", ...}}` |
| wijzigen (1..n) | `PATCH .../values/...` | `{"values": {...}}` |
| verwijderen (1) | `DELETE .../values/.../{key}` | - |
| verwijderen (n) | `POST .../values/.../:delete` | `{"keys": [...]}` |
| alles verwijderen | `DELETE .../values/...` | - |

Bulk is de basisvorm: een mapping van lengte 1 is het enkelvoudige geval, dus er is geen
aparte enkelvoudige variant. `POST .../:delete` volgt de bestaande actie-conventie
(`:upsert-deployment`, `:refresh`).

**Toevoegen faalt op een bestaande naam, wijzigen en verwijderen op een ontbrekende.**
Anders overschrijft een typefout stil een waarde, en omdat de opgeslagen vorm versleuteld
is, ziet niemand dat terug in de diff.

**Waarden worden nooit teruggegeven.** Er is geen leesendpoint: een waarde teruglezen zou
precies het geheim uitleveren dat deze endpoints versleuteld houden. Toevoegen, wijzigen
en verwijderen op naam hoeven dat ook nergens voor.

### Aliassen hebben geen deploymentniveau

`user-env-vars` heeft endpoints op **beide** niveaus, `aliases` **alleen op
componentniveau**. Dat is geen omissie maar de vorm van het projectbestand: het
`deployment-component`-object in `opi/schemas/project_v2.json` heeft
`additionalProperties: false` en geen `aliases`-property.

Er is bewust voor gekozen (8 augustus 2026, opdrachtgever) daar **geen schemawijziging**
voor te doen. Aliassen op deploymentniveau zou een nieuwe `x-zad-schema-version` plus een
legacy-patch vragen, en het aliasmechanisme verdwijnt op termijn sowieso ten gunste van
env-vars; die investering is het niet waard.

Een aanroep op `/services/aliases/values/deployment/...` geeft daarom een 404: er is geen
route, en dus ook geen schrijfactie die het schema alsnog zou breken. Om te voorkomen dat
een client dat pas uit die 404 hoeft af te leiden, meldt `GET /api/v2/services` het nu per
service:

```json
{"name": "aliases",        "value_targets": ["component"]}
{"name": "user-env-vars",  "value_targets": ["component", "deployment-component"]}
```

## De opslagvormen

Dit is het lastigste deel, en waar een naïeve implementatie een geheim in plaintext in git
zet. De twee velden worden **verschillend** opgeslagen:

| veld | opslag |
|---|---|
| `user-env-vars` | **één** AGE-blok voor de hele set, met `KEY=value`-regels erbinnen |
| `aliases` | een mapping met leesbare namen, en **elke waarde apart** AGE-versleuteld |

Wijzigen is in beide gevallen **ontsleutelen -> muteren -> opnieuw versleutelen**. Een blok
kun je niet per regel bewerken, en een aliaswaarde niet zonder de andere te laten staan.

De service declareert zelf welke van de twee vormen hij gebruikt
(`owned_values_storage`, `opi/services/catalog/base.py`), en
`opi/services/component_values.py` is de enige implementatie van beide vormen.

## Fail-closed

Deze twee valkuilen zaten er al, en de keuze hierin is bewust:

1. **`KeyValueConverter._maybe_encrypt` vangt breed af en geeft dan de plaintext terug**
   met een warning (`opi/forms/editables/converters.py`). Voor een formulier
   verdedigbaar - de invuller staat ernaar te kijken en kan het opnieuw proberen. Voor een
   API-schrijfpad is het fail-open op een geheim. **Keuze: niet hergebruiken.**
   `component_values.py` is een eigen, fail-closed schrijfhelper. De converter blijft
   ongewijzigd; die is het lees/schrijfpad van de wizard en zijn faalgedrag veranderen is
   een aparte wijziging met een eigen blast radius.
2. **`UserEnvVarsEncryptGenerator` liep alleen over `components[*]`**, terwijl dezelfde
   service ook `deployments[*]/components[*]/user-env-vars` bezit. Die generator is de
   vangnetlaag voor wanneer er niets anders versleuteld heeft (geen contextdata, een
   handmatig bewerkt bestand) - en een vangnet dat de helft van de lagen dekt, laat de
   andere helft in plaintext in git belanden. **Keuze: gerepareerd**, de generator loopt nu
   over beide lagen.

Concreet betekent fail-closed hier:

- geen publieke sleutel op het project -> er wordt **niets** geschreven, de taak faalt;
- een opgeslagen waarde die zich als AGE-versleuteld aandient maar niet ontsleutelt ->
  fout, niet "geef de ciphertext maar door" (dat zou hem bij de volgende schrijfactie als
  plaintext terugzetten).

## Geen commit zonder wijziging

AGE is niet deterministisch: dezelfde waarde opnieuw versleutelen geeft andere ciphertext.
Zonder maatregel zou elke aanroep een commit in `zad-projects` opleveren, ook als er niets
veranderde. De vergelijking gebeurt daarom **na ontsleutelen**, op de platte waarden. Levert
dat hetzelfde op, dan geeft de wijzigingsfunctie `None` terug: geen commit, geen uitrol, en
`changed: false` in het taakresultaat.

### Wat er daarom geweigerd wordt aan de randen van een waarde

De no-op-detectie hierboven werkt alleen als een waarde er hetzelfde uit komt als hij erin
ging. Twee normalisaties op het leespad doen dat niet:

| normalisatie | waar | raakt |
|---|---|---|
| `decrypt_age_content_sync` doet `.strip()` op de plaintext (dat moet: de armored vorm eindigt op een newline) | beide vormen | witruimte aan begin/eind |
| `validate_and_parse_env_vars` leest `KEY=value` zoals een shell dat doet en haalt één paar omringende aanhalingstekens weg | alleen `BLOCK` (`user-env-vars`) | `"q"` -> `q`, `'q'` -> `q` |

Een waarde die daardoor verandert zou twee beloftes tegelijk breken: hij komt anders terug
dan hij geschreven is, en de opgeslagen set zou nooit gelijk zijn aan de gevraagde - dus
elke aanroep zou opnieuw committen in `zad-projects`, precies de churn die hierboven
uitgesloten is.

De API weigert zulke waarden daarom met een **422**, voordat er iets in de wachtrij komt:

- randwitruimte (`" x "`, `"x "`, `" x"`, een waarde die alleen uit spaties bestaat) op
  **beide** velden;
- een paar omringende aanhalingstekens (`"q"`, `'q'`, `""`) alleen op `user-env-vars`;
  bij aliassen blijven die gewoon staan.

Aanhalingstekens *binnen* een waarde (`say "hi" now`), een `=` in de waarde en een lege
waarde zijn gewoon toegestaan. De melding noemt de naam en nooit de waarde.

Bewust niet gekozen: de waarde stil normaliseren (dan krijgt de workload iets anders dan
gevraagd), of hem quoten/escapen op het schrijfpad (zelfde probleem, plus een tweede
opslagvorm om te lezen). De controle staat in `validate_value_for_storage`
(`opi/services/component_values.py`) en wordt zowel door de route als door
`ProjectManager.set_component_values` aangeroepen, zodat er geen schrijfpad omheen loopt.
Toetsen: `TestStorageFidelity` in `tests/test_component_values.py`, plus de 422-toetsen in
`tests/test_component_values_api.py` en de weigering op het schrijfpad in
`tests/test_component_values_manager.py`.

## Twee lagen blijven apart

`user-env-vars` bestaat op twee niveaus die bij het uitrollen samengevoegd worden
(deployment-component wint per sleutel). Die merge betekent alleen iets zolang beide
waarden op hun eigen plek staan. Eén service die beide lagen bezit is precies de situatie
waarin je ze per ongeluk naar één plek schrijft, dus dat is apart vastgelegd in
`tests/test_component_values_manager.py`.

## Rollout en asynchroon

Net als de omliggende endpoints:

- **202** met een taak-id en een `Location`-header; het resultaat via `/api/tasks/{id}`.
- **`?rollout=false`** slaat de uitrol over: de wijziging gaat wel het projectbestand in en
  wordt gecommit, maar er worden geen manifesten gegenereerd en er bereikt niets het
  cluster. Uitrollen doe je dan later met
  `POST /api/v2/projects/{project}/:refresh`. Het taaktype `configure_service_values` staat
  in `DEFERRABLE_TASK_TYPES`.

Schrijven gaat altijd via `ProjectStore` (`ProjectManager.mutate_and_commit_project`), nooit
rechtstreeks YAML.

## Validatie

- **Namen** moeten voldoen aan `^[A-Za-z_][A-Za-z0-9_]*$` - voor aliassen net zo goed als
  voor env-vars: een alias wórdt een omgevingsvariabele.
- **Waarden** mogen geen newline, carriage return of null-byte bevatten, want env-vars gaan
  als `KEY=value`-regels over de lijn.
- **Waarden die de opslagvorm niet byte-voor-byte overleven worden geweigerd** (zie
  hieronder).
- Beide leveren een **422** op voordat er iets in de wachtrij komt. Een naam die in het
  *pad* staat (de enkelvoudige delete) wordt op dezelfde manier afgekeurd.
- Een onbekend project, component of deployment is een **404** op het verzoek zelf, niet een
  taak die minuten later faalt.
- Foutmeldingen noemen wel de naam, nooit de waarde.

## Voorbeelden

Alle aanroepen gaan met de projectsleutel (`X-API-Key`).

```bash
KEY=<project api key>
BASE=https://zad.sandbox.rijksapp.dev/api/v2/projects/mijnproject/services
```

### Env-vars op een component

```bash
# toevoegen (meerdere tegelijk)
curl -X POST "$BASE/user-env-vars/values/component/backend" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"values": {"DATABASE_TIMEOUT": "30", "FEATURE_X": "on"}}'

# wijzigen
curl -X PATCH "$BASE/user-env-vars/values/component/backend" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"values": {"DATABASE_TIMEOUT": "60"}}'

# er een verwijderen
curl -X DELETE "$BASE/user-env-vars/values/component/backend/FEATURE_X" -H "X-API-Key: $KEY"

# er meerdere verwijderen
curl -X POST "$BASE/user-env-vars/values/component/backend/:delete" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"keys": ["DATABASE_TIMEOUT", "FEATURE_X"]}'

# alles weg
curl -X DELETE "$BASE/user-env-vars/values/component/backend" -H "X-API-Key: $KEY"
```

### Env-vars op een deployment-component (de override)

```bash
curl -X POST "$BASE/user-env-vars/values/deployment/deployment-1/component/backend" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"values": {"LOG_LEVEL": "debug"}}'
```

Deze waarde wint bij het uitrollen van de gelijknamige waarde op het component; de
component-waarde blijft gewoon staan.

### Aliassen (alleen op een component)

```bash
curl -X POST "$BASE/aliases/values/component/backend" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"values": {"POSTGRES_HOST": "$DATABASE_SERVER_HOST", "POSTGRES_PORT": "$DATABASE_SERVER_PORT"}}'
```

### Meerdere wijzigingen, één uitrol

```bash
curl -X POST "$BASE/user-env-vars/values/component/backend?rollout=false" ...
curl -X POST "$BASE/aliases/values/component/backend?rollout=false" ...
curl -X POST "https://.../api/v2/projects/mijnproject/:refresh" -H "X-API-Key: $KEY"
```

## Waar het staat

| onderdeel | bestand |
|---|---|
| opslagvormen, versleutelen/ontsleutelen, operaties | `opi/services/component_values.py` |
| declaratie van de opslagvorm per service | `opi/services/catalog/base.py` (`ValueStorage`, `owned_values_storage`) |
| de routes (registry-gedreven) | `opi/api/v2/router.py` (`_register_service_values_routes`) |
| de schrijfactie | `ProjectManager.set_component_values` |
| de taak | `opi/core/task_handlers_components.py` (`handle_configure_service_values`) |
| toetsen | `tests/test_component_values.py`, `tests/test_component_values_api.py`, `tests/test_component_values_manager.py` |

Een service die in de toekomst ook een sleutel/waarde-property bezit, krijgt zijn endpoints
door `owned_values_storage` te declareren - er staat nergens een servicenaam in de router.

## Afhankelijkheden

De `age`-binary (versleutelen/ontsleutelen) en een project met een AGE-sleutelpaar in
`config`. Beide zijn er in elk echt project; de toetsen slaan zichzelf over als `age`
ontbreekt.
