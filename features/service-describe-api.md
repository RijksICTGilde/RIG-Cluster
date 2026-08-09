# Een dienst beschrijven via de API

Iemand die de ZAD-portal nooit heeft gezien -- een CLI, een script, een agent -- moet via
API-aanroepen kunnen achterhalen welke diensten er zijn, wat ze doen, waar je ze toepast en
welke omgevingsvariabelen ze opleveren. De dienstenlijst bestond al; wat ontbrak was de
aard van een dienst, de variabelen en de uitleg.

## De twee routes

```
GET /api/v2/services              alle diensten, genoeg om te KIEZEN
GET /api/v2/services/{name}       één dienst, genoeg om hem TOE TE PASSEN
```

Beide zijn publiek en projectonafhankelijk, en dat is een bewuste keuze: de describe geeft
geen projectgegevens en geen geheimen, alleen namen van variabelen en de opbouw van het
platform. Wie dit wil afschermen moet ook de bestaande lijst afschermen; de twee horen bij
elkaar. Alle omschrijvingen zijn Nederlands, net als de rest van het platform.

### De lijst

Per dienst: `name`, `description`, `configurable`, `targets`, `value_targets`,
`config_schema_version`, en sinds RC-59 ook:

| veld | waarom je het nodig hebt om te kiezen |
|---|---|
| `kind` | `user` mag je zelf kiezen, `system` draait het platform altijd |
| `binding` | vinkt een component dit aan (`component`) of de hele deployment (`deployment`) |
| `hidden` | een variant die het platform zelf kiest; bied hem niet aan |
| `requires` | wat er eerst moet staan, als yaml-paden |

### De describe

Alles wat een client nodig heeft om de dienst toe te passen:

1. **Wat het is** - naam, omschrijving, `kind`, `binding`, `hidden`, en `explanation`: de
   volledige uitleg als markdown (zie `features/service-help-texts.md`).
2. **Waar je het toepast** - `layers`, per laag met `yaml_path` (waar het blok in het
   projectbestand landt), `roles`, `config_endpoint`, `has_form` en `form_exempt_reason`.
   Ook de lagen die bewust geen formulier hebben: "hier kan het wel via de API maar
   expres niet via een formulier" is precies wat een API-client moet weten.
3. **Hoe je het instelt** - `config_schema_version` plus per laag het endpoint dat daar
   schrijft. Het JSON-schema zelf staat al in de OpenAPI-beschrijving van dat endpoint;
   hier staat de verwijzing, geen tweede kopie.
4. **Wat het oplevert** - `variables`: per variabele `name`, `description`, `source`
   (`secret` of `direct`), `aliases` en `secret_key`. Een dienst zonder variabelen geeft
   een lege lijst, nooit een ontbrekend veld.
5. **Wat het nodig heeft** - `requires`.
6. **Wat er gebeurt als het weggaat** - `cleanup_strategy` (`none` / `immediate` /
   `deferred`) en `backup_label`.

Een onbekende naam geeft 404 met de geldige namen erbij.

### `variables` is maar de helft

`variables` is wat het *platform* levert. Wat een project zelf zet loopt via de diensten
`user-env-vars` en `aliases`, en die staan niet in de `variables` van de andere diensten.
De uitleg van die twee zegt dat expliciet, met de route waarmee je ze beheert.

## Geen tweede documentatiesysteem

Elk veld is een projectie van wat de dienst al declareert: `ServiceDefinition`,
`VariableDefinition`, `config_model`, `config_layers()`, `form_exempt_layers`. Er komt
geen prozaveld bij dat alleen voor de API bestaat, want dat gaat uit de pas lopen met het
gedrag, en een verkeerd antwoord is erger dan geen antwoord. Kan iets niet worden afgeleid,
dan mist de declaratie dat feit en hoort het dáár bij.

`tests/test_service_describe_api.py` bewaakt dat: elke `ServiceType` levert een volledige
describe op, en elk veld wordt tegen de registry vergeleken in plaats van tegen een kopie
van het verwachte antwoord.

## Databaseschema's als eigen deelbron

```
GET    /api/v2/projects/{p}/services/postgresql-database/schemas
POST   /api/v2/projects/{p}/services/postgresql-database/schemas
DELETE /api/v2/projects/{p}/services/postgresql-database/schemas/{postfix}
```

Schema's stonden alleen in de dienstconfig, en toevoegen of weghalen kon dus alleen door de
hele config met een PUT te vervangen. Dat is precies waar dat gevaarlijk is: RC-17 heeft
gekozen dat een schema uit de lijst halen de data **niet** weggooit -- verwijderen markeert
-- maar dat markeren was een vinkje in het formulier. Een client die de config in zijn
geheel terugschrijft met één schema minder laat dat schema gewoon uit het bestand vallen,
en niets in het verzoek gaat daarover.

**De lijst begint bij het standaardschema.** Elke database krijgt er een, en dat is het
schema waar de meeste mensen het over hebben -- maar het staat nergens in het
projectbestand: het wordt afgeleid uit project- en deploymentnaam en aangeboden als
`DATABASE_SCHEMA` (alias `APP_DATABASE_SCHEMA`). Een lijst met alleen het `schemas:`-blok
laat juist dat weg. Het staat als eerste regel in de lijst, met een lege `postfix`,
`is_default: true`, en het is niet adresseerbaar voor verwijderen.

Per regel: `postfix`, `is_default`, `description`, `marked_for_deletion`, `variable_name`,
`aliases` en de volledige schemanaam per deployment.

**De namen worden uitgerekend, niet nagevertelt.** Beide soorten gaan door
`opi/utils/naming.py`, want ze gedragen zich verschillend bij de 63-tekengrens van
PostgreSQL: het standaardschema wordt stil afgekapt (`generate_database_schema`), een extra
schema faalt daar juist hard (`generate_extra_database_schema` gooit een `ValueError`, en
dan is `schema_name` `null`). Wie de naam zelf samenplakt krijgt bij lange namen een schema
dat niet bestaat. Dat verschil is de reden dat deze lijst een eigen route verdient.

| handeling | wat er gebeurt |
|---|---|
| `POST` | schema erbij; het 202-antwoord draagt meteen de volledige naam per deployment en de variabelenaam |
| `POST` met een bestaande actieve postfix | 409 |
| `POST` met een gemarkeerde postfix | komt terug, met zijn data |
| `DELETE` | markeert; schema en data blijven, de variabele wordt niet meer aangeboden |
| `DELETE?forget=true` | haalt de regel uit het bestand; de data blijft ook dan staan, maar niets legt nog vast dat het schema er is |
| `DELETE` van iets dat al gemarkeerd is | `changed: false`, geen commit, geen uitrol |

Niets in deze API laat ooit een schema in de database vallen.

De controles bij het opslaan -- uniciteit, de 63-tekengrens, botsende variabelenamen --
blijven waar ze zijn: bij het opslaan, hetzelfde punt dat de wizard raakt. Deze routes
geven die fouten door als 422 in plaats van ze over te doen.

### De naamgeving van een postfix

Zodra een API schema's laat toevoegen komt de postfix niet meer alleen uit een formulier,
maar ook uit een script dat de regels niet kent. Drie dingen zijn daarvoor dichtgezet:

- **Eén definitie.** `SCHEMA_POSTFIX_PATTERN` en `SCHEMA_POSTFIX_MAX_LENGTH` staan in
  `opi/utils/naming.py`, naast de functies die de samengestelde naam bouwen, en het
  configmodel, `SchemaPostfixValidator` en het API-verzoekmodel lezen ze daar alle drie
  uit. Het maximum (32) **vervangt de samengestelde 63-controle niet** en kan dat ook
  niet: hoeveel ruimte er is hangt af van de project- en deploymentnaam. Het zorgt er
  alleen voor dat een postfix van 200 tekens faalt op zijn lengte, in plaats van als een
  klacht over een naam die de aanroeper nooit geschreven heeft.
- **Weigeren, niet normaliseren.** `Rapportage` geeft een 422 met de reden. Stil
  omzetten naar `rapportage` zou iets anders opslaan dan er gevraagd is, en de aanroeper
  zou dat pas merken aan de schemanaam in zijn database.
- **De samengestelde controle draait ook als er een deployment bijkomt.** Die stond in
  `UniqueSchemaEnforcer` en keek alleen naar de deployments die er op dat moment waren,
  dus een postfix die vandaag past werd stil ongeldig zodra er een langere deploymentnaam
  bijkwam -- en dat bleek dan pas bij het uitrollen. `validate_database_schema_names`
  draait hem nu in `validate_project_structure`, waar elke opslag langskomt, dus het
  toevoegen van die deployment wordt geweigerd met een melding die zowel de deployment als
  de postfix noemt. Een gemarkeerd schema telt niet mee: dat is op weg naar buiten.

Wat **niet** in deze taak is opgelost: het standaardschema kapt stil af op 63 tekens
zonder hash, dus twee lange deploymentnamen kunnen tot dezelfde schemanaam afkappen. Zie
`docs/KNOWN-ISSUES.md`; een naamgevingsregel wijzigen raakt bestaande databases en vraagt
een eigen migratie.

`POST` en `DELETE` zijn asynchroon (202 met een task-id) en nemen `rollout=false`, net als
de andere schrijfroutes.

## Testen

```bash
cd operations-manager/python
uv run pytest tests/test_service_describe_api.py tests/test_database_schemas_api.py -q
```

## Verwant

- `features/service-help-texts.md` - de uitleg zelf, en waarom hij markdown is
- `features/service-config-api.md` - de generieke configroutes waar de describe naar wijst
- `features/component-values-api.md` - `user-env-vars` en `aliases`
- `features/api-documentation-grouping.md` - waar een agent begint: de OpenAPI-beschrijving
