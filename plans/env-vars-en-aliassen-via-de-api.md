# Env-vars en aliassen via de API

Status: plan, 8 augustus 2026. Niet gebouwd. Geschreven na een eerste poging die op de
verkeerde basis is gebouwd en daarom niet bruikbaar is (zie "Wat er al ligt").

## Waarom

`user-env-vars` en `aliases` zijn de enige twee geregistreerde services zonder ook maar één
API-endpoint. Ze zijn alleen via de wizard en de bewerkmodals te zetten. Voor een CLI of
automatisering is dat een gat: je kunt een project aanmaken en componenten toevoegen via de API,
maar niet zeggen welke omgevingsvariabelen erin moeten.

Gemeten op de live spec van de sandbox:

```
services met endpoints: attachments, authorization-wall, cross-domain-access, health-check,
  invite, keycloak, metrics-scraper, minio-storage, namespace-postgresql-database,
  persistent-storage, postgresql-database, publish-on-web, redis, sleep-mode, temp-storage
services zonder endpoints: user-env-vars, aliases
```

## Waarom ze er nu geen hebben, en waarom dat geen vergissing is

Niet vergeten, maar bewust uitgesloten. In `opi/api/v2/router.py`:

```python
def _accepts_config_at(service, layer: ConfigLayer) -> bool:
    if service.owned_property is not None:
        # A service that owns a plain project-file property (user-env-vars, aliases) has
        # no config block in any ``services:`` list, so this endpoint -- which reads and
        # writes exactly that block -- has nothing to address. Generating a route for it
        # would let a caller write a config block that nothing ever reads (RC-25).
        return False
```

De generieke config-routes (`PUT/DELETE /projects/{p}/services/{svc}/config/...`) schrijven het
config-blok in de `services:`-lijst. Deze twee services schrijven een **eigen property op het
component** (`user-env-vars:`, `aliases:`). De generieke machinerie zou dus het verkeerde ding
adresseren, en daarom slaat hij ze over.

**Het gat is niet dat de uitsluiting fout is; het gat is dat er nooit endpoints voor de
owned-property-vorm gebouwd zijn.** Dat is wat deze taak doet.

## Wat er gevraagd is

Per component en per deployment/component:

- **toevoegen** van een of meer key/values
- **wijzigen** (patch) van een of meer bestaande waarden
- **verwijderen** van een of meer waarden op key
- **alles verwijderen** op dat niveau

Voor beide velden, env-vars en aliassen.

## De opslagvormen, en waarom dat het lastigste deel is

Dit is waar een naïeve implementatie een geheim in plaintext in git zet. De twee velden hebben
**verschillende** vormen:

| veld | opslag |
|---|---|
| `user-env-vars` | **één** AGE-blok voor de hele set, met `KEY=value`-regels erbinnen |
| `aliases` | een mapping met leesbare namen, en **elke waarde apart** AGE-versleuteld |

Bron: `opi/services/catalog/user_env_vars/config_model.py` (drie legale vormen: AGE-blok, plat
`KEY=value`-blok, en een legacy mapping) en `opi/services/catalog/aliases/config_model.py`
("stored as a mapping on the component, with each value AGE-encrypted independently").

Wijzigen is in beide gevallen: **ontsleutelen -> toevoegen/vervangen/verwijderen -> opnieuw
versleutelen**. Nooit gedeeltelijk, nooit met een plaintext-terugval.

### Twee valkuilen die al gevonden zijn

1. **`KeyValueConverter._maybe_encrypt` vangt breed af en geeft dan de plaintext terug** met een
   warning. Voor een formulier verdedigbaar, voor een API-schrijfpad is dat fail-open op een
   geheim. Niet hergebruiken zonder dat gedrag te veranderen.
2. **`UserEnvVarsEncryptGenerator` loopt alleen over `components[*]`.** Het
   deployment-componentniveau raakt hij niet aan, dus precies de helft van wat gevraagd is zou
   onversleuteld blijven.

Kies bewust: of een eigen fail-closed schrijfhelper, of die twee eerst fail-closed maken. Leg de
keuze vast in de feature-doc.

## Een beslissing die genomen moet worden: aliassen op deploymentniveau

`opi/schemas/project_v2.json` heeft bij `deployment-component`:

```
additionalProperties: False
properties: [..., 'env-vars', 'user-env-vars', ...]   <- geen 'aliases'
```

Dus **env-vars kan op beide niveaus, aliassen alleen op componentniveau**. Dat strookt met de
registry: `user-env-vars` declareert `config_layers = ['component', 'deployment-component']`,
`aliases` alleen `['component']`.

Aliassen op deploymentniveau toevoegen is daarom geen implementatiedetail maar een
**schemawijziging**: nieuw veld in `deployment-component`, `x-zad-schema-version` omhoog, en een
legacy-patch erbij, precies zoals `features/project-schema-versions.md` voorschrijft. Zonder die
stap weigert de opstartcontrole (`check_schema_versions`) te booten.

**Besloten (8 augustus, opdrachtgever): geen schemawijziging.** Aliassen blijven op
componentniveau, env-vars kunnen op beide niveaus. Dat was ook het oorspronkelijke idee achter
aliassen, en het aliasmechanisme verdwijnt op termijn sowieso; daar nu een nieuwe schemaversie
en een legacy-patch voor optuigen is de investering niet waard.

Praktisch betekent dat:

- `user-env-vars`: endpoints op **componentniveau en deployment/componentniveau**.
- `aliases`: endpoints op **alleen componentniveau**.

Dat is geen omissie maar de vorm van het projectbestand, en het hoort ook zo in de feature-doc
te staan, zodat niemand het later als gat aanmerkt. Een aanroep op het deploymentniveau voor
aliassen hoort een nette 404 of 422 te geven met die uitleg, niet een 500 of een stille
schrijfactie die het schema alsnog breekt. Neem daar een toets voor op.

## Het endpointschema

Hang ze aan de servicelaag, niet aan `components`. Dat is waar de rest van de servicefunctionaliteit
staat en het is wat de opdrachtgever ook schetste:

```
componentniveau            /api/v2/projects/{project}/services/{svc}/values/component/{component}
deployment/componentniveau /api/v2/projects/{project}/services/{svc}/values/deployment/{deployment}/component/{component}
```

met `{svc}` = `user-env-vars` of `aliases`. Het tweede pad bestaat alleen voor `user-env-vars`,
zie de beslissing hierboven. Kies één naamgeving en pas hem op beide toe. Volg
verder de bestaande vorm van `/services/{svc}/config/component/{component_name}` zodat het rijtje
consistent blijft; `attachments` is het precedent voor endpoints per item naast de config-routes.

| operatie | methode + pad | body |
|---|---|---|
| toevoegen (1..n) | `POST .../values/...` | `{"values": {"NAAM": "waarde", ...}}` |
| wijzigen (1..n) | `PATCH .../values/...` | `{"values": {...}}` |
| verwijderen (1) | `DELETE .../values/.../{key}` | - |
| verwijderen (n) | `POST .../values/.../:delete` | `{"keys": [...]}` |
| alles verwijderen | `DELETE .../values/...` | - |

Bulk is de basisvorm: een mapping van lengte 1 is het enkelvoudige geval, dus er is geen aparte
enkelvoudige variant nodig. `POST .../:delete` volgt de bestaande actie-conventie
(`:upsert-deployment`, `:refresh`). `toevoegen` faalt op een bestaande naam, `wijzigen` en
`verwijderen` op een ontbrekende - anders kan een typefout stil een waarde overschrijven.

## Wat er verder verplicht is

- **`rollout` honoreren.** De v2-router gebruikt hem 46 keer (`RolloutQuery`, `NoDeferQuery`,
  `_reject_deferred_rollout`, `NON_DEFERRABLE_REASONS` in `opi/core/task_rollout.py`). Deze
  endpoints wijzigen het projectbestand en rollen daarna uit, dus ze horen `rollout=false` te
  accepteren zoals de rest. Verzin geen eigen variant.
- **Asynchroon**, zoals de omliggende endpoints: 202 met een taak-id en `Location`, resultaat via
  `/api/tasks/{id}`.
- **Schrijven gaat via `ProjectStore`.** Nooit rechtstreeks YAML schrijven.
- **Sleutelnamen valideren** tegen `^[A-Za-z_][A-Za-z0-9_]*$`, voor aliassen net zo goed als voor
  env-vars: een alias wordt een omgevingsvariabele. `ENV_VAR_NAME` staat al in het aliases-config
  model. Waarden met een newline of null-byte weigeren, want env-vars gaan als `KEY=value`-regels
  over de lijn.
- **Een no-op mag niet committen.** AGE is niet deterministisch, dus een waarde patchen naar
  wat er al staat zou het blok elke aanroep opnieuw schrijven en zo een commit per aanroep in
  `zad-projects` maken. Vergelijk na ontsleutelen.
- **Lees `instructions/services.md`** voor het servicecontract voordat je begint, en
  `instructions/service-review-checklist.md` voor de review.

## Raakvlak met een openstaande bug

De sandboxrun van 7 augustus vond dit (`docs/sandbox-run-2026-08-07-rc54.md`, bevinding 5):

```
test_sandbox_env_vars_aliases_ui.py::test_deployment_component_env_vars_override_saves
E  AssertionError: writing the deployment-component override wiped the component-level
   user-env-vars; the two layers must be stored separately for the merge to have
   anything to merge
```

Een deployment-override wist dus vandaag de env-vars van het component. Dat is exact het pad dat
deze API ook gaat schrijven. Bouw er niet blind overheen: bepaal eerst of die bug in de
gedeelde schrijflaag zit die je gaat gebruiken. Zit hij daar, dan hoort hij in deze taak thuis
en is de bestaande e2e-toets meteen je bewijs. Zit hij in de UI-laag, meld dat dan expliciet en
laat hem staan.

## Toetsen

Verplicht, niet optioneel:

- Routes: happy path, verkeerde sleutel (401), onbekend project/component/deployment (404),
  ongeldige payload en ongeldige sleutelnaam (422), bulk, en `rollout=false`.
- Opslagvorm: dat `user-env-vars` als **één** AGE-blok landt en `aliases` **per waarde**
  versleuteld, allebei tegen de echte `age`-binary. Dat twee identieke plaintexts verschillende
  ciphertext geven bewijst dat het echt per waarde gaat.
- Round-trip met de UI-kant: wat de API schrijft moet de wizard/editor kunnen lezen en andersom.
  Dit is waar een afwijkende vorm stilletjes de UI breekt.
- Fail-closed: zonder publieke sleutel wordt er niets geschreven.
- Geen churn: dezelfde waarde patchen levert geen commit op.
- De spec meten op `app.openapi()`, niet aannemen: alle paden en methodes die je toevoegt.

Daarna: `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright`, en
`uv run pytest tests/ -q` (~6540 tests, ~4 minuten). Meld de getallen die je echt zag.

## Documentatie

Een feature-doc in `features/` (kebab-case): wat het is, curl-voorbeelden per operatie voor
beide velden en beide niveaus, de gekozen URL/payload-vorm met de reden, de opslagvormen, en de
keuze rond aliassen op deploymentniveau.

## Wat er al ligt, en waarom het niet bruikbaar is

Er is een eerdere poging: branch `worktree-agent-a07d7595b62af2eb6`. **Niet mergen.** Hij is
afgetakt van `main`, dat 574 commits achterloopt, en daar bestaan de service-pakketten
(`catalog/user_env_vars/`, `catalog/aliases/`), `instructions/services.md` en de
`rollout`-parameter allemaal niet. Concreet gevaar: op die basis zijn aliassen plaintext, dus
die code blind overnemen zet aliaswaarden onversleuteld in git.

Wat er wél uit te halen valt, als referentie:

- het URL- en payload-schema hierboven komt eruit en is bruikbaar;
- de vijf operaties en de 404/422-poorten;
- de twee valkuilen die hierboven staan (fail-open converter, generator die deploymentniveau
  overslaat);
- een gevonden lek dat los van deze taak gerepareerd hoort te worden: `KeyValueConverter` logde
  de waarde op INFO, wat voor env-vars al een lek was.

Begin met een verse worktree van `alles-groen-op-de-sandbox-met-de-echte-projectbest`, niet van
`main`. Dat is de fout die de eerste poging onbruikbaar maakte.

## Volgorde

1. Lees `instructions/services.md` en de twee `config_model.py`'s.
2. Bepaal of bevinding 5 in de schrijflaag zit die je gaat gebruiken.
4. Schrijfhelper met ontsleutel/wijzig/versleutel, fail-closed, voor beide vormen.
5. Endpoints, in de servicelaag, met `rollout` en de asynchrone taak.
6. Toetsen zoals hierboven, inclusief de round-trip met de UI.
7. Feature-doc, en `features/`-index bijwerken als die bestaat.
