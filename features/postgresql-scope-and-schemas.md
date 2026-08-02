# PostgreSQL: scope-keuze en meerdere schema's

De `postgresql-database`-service kan sinds RC-17 twee dingen die daarvoor niet konden:
een **plaatsingskeuze** (`scope`) en **extra schema's** binnen dezelfde database.

## Scope: gedeeld of eigen cluster

De service heeft een veld `scope` in zijn projectconfig:

| Waarde | Betekenis |
|---|---|
| `shared` (default) | Een database op de gedeelde clusterinstantie. Dit is het gedrag van voorheen; een project dat niets zet valt hierop terug. |
| `project` | Een eigen CNPG-cluster per project in de infrastructuurnamespace, gedeeld door alle deployments. Dit is wat de aparte `namespace-postgresql-database`-service ook doet. |

De config is een Pydantic *discriminated union* op `scope`: een veld dat alleen bij
een eigen cluster hoort (`storage`, `instances`, `image`, `registry`, `privileges`,
`postInitSQL`, `resources`) faalt op `scope: shared` met een leesbare fout in plaats van
stil genegeerd te worden.

```yaml
services:
  - postgresql-database:
      config:
        scope: project        # eigen CNPG-cluster
        storage: 20Gi
        instances: 2
```

`namespace-postgresql-database` blijft voorlopig bestaan en werken; `scope: project`
levert hetzelfde resultaat. De plaatsingskeuze wordt centraal bepaald in
`opi/services/postgres_scope.py` (`project_uses_dedicated_postgres`,
`get_dedicated_postgres_config`); alle poorten (provisioning, infrastructuurnamespace,
superuser-credentials, backup-brontype, delete-opruiming) routeren daardoorheen.

> Scope `deployment` (een eigen cluster per deployment) en het verplaatsen van de
> toegewijde database naar de projectnamespace zijn bewust **niet** in deze ronde
> gebouwd.

## Meerdere schema's

Naast het standaardschema kan een project extra schema's declareren. Ze zijn
**project-breed**: elke deployment krijgt dezelfde schema's, elk in zijn eigen database.

```yaml
services:
  - postgresql-database:
      config:
        schemas:
          - postfix: rapportage
            description: Rapportagetabellen
          - postfix: audit
            description: Auditlog
```

Per deployment wordt de volledige schemanaam `{project}_{deployment}_{postfix}`. Het
standaardschema en zijn variabele `DATABASE_SCHEMA` blijven ongewijzigd, dus een project
zonder extra schema's verandert niet.

### Variabelen in de pod

Per extra schema komt er één variabele bij, met een `APP_`-alias:

| Variabele | Waarde |
|---|---|
| `DATABASE_SCHEMA` | Het standaardschema (ongewijzigd) |
| `DATABASE_SCHEMA_{POSTFIX}` | De volledige naam van het extra schema (postfix in hoofdletters) |

De `search_path` van de databaserol staat op `[standaardschema, extra schema's..., public]`
(standaardschema voorop), dus een applicatie die niets instelt werkt door, en een die een
extra schema wil gebruiken kan een gekwalificeerde naam of `SET search_path` gebruiken.
Er komt bewust **geen** aparte connectiestring per schema.

De deployment-brede read-only rol (`_ro`, gebruikt door de database-console) krijgt SELECT
op elk schema, zodat de console alle schema's kan tonen.

### Naamgeving faalt luid

Bij het opslaan (wizard of API) worden drie dingen streng gecontroleerd, met een fout op
het schema-veld zelf:

- een dubbele postfix;
- een volledige schemanaam die voor een bestaande deployment boven de 63 tekens uitkomt
  (twee lange postfixen mogen na afkappen niet samenvallen, dus de volledige naam wordt
  gecontroleerd, niet de postfix alleen);
- een variabelenaam die botst met een bestaande databasevariabele.

### Verwijderen is markeren

Een schema uit de lijst halen zou data weggooien. In plaats daarvan is er per schema een
**markeer voor verwijdering**-optie: het schema stopt met beheerd worden en zijn variabele
verdwijnt, maar het schema en zijn data blijven in de database staan. Verwijderen gebeurt
nooit automatisch.

## Generaties, klonen en backups

Extra schema's leven in dezelfde database als het standaardschema. De generatie zit in de
*databasenaam* (`proj_dep_v2`), niet in de schemanaam, dus een kloon neemt alle schema's
mee. De kloonweg (`clone_schema`) kopieert elk extra schema via dezelfde single-schema
pijplijn als het standaardschema. Backups dumpen de hele database, dus extra schema's
zaten daar al in.

## Waar het zit

| Onderdeel | Bestand |
|---|---|
| Configmodel (scope + schemas) | `opi/services/catalog/postgresql_database/config_model.py` |
| Gedeelde CNPG-velden | `opi/services/catalog/shared/postgres.py` |
| Plaatsing + schema-lezer | `opi/services/postgres_scope.py` |
| Naamgeving | `opi/utils/naming.py` (`generate_extra_database_schema`, `generate_schema_variable_name`) |
| Provisioning | `opi/manager/database_manager.py` |
| Kloon | `opi/connectors/postgres.py` (`clone_schema`) |
| Secret/variabelen | `opi/utils/secrets.py` (`DatabaseSecret`) |
| UI | `opi/services/catalog/postgresql_database/{editables,visualizers}.py` + `config_form_section` |
| Save-validatie | `opi/forms/editables/validators.py` (`SchemaPostfixValidator`), `opi/forms/editables/enforcers.py` (`UniqueSchemaEnforcer`) |
