# Een database is alleen te openen door zijn eigen rollen

**Status**: geimplementeerd, standaard `on`
**Taak**: RC-285, RC-519

## Wat het is

PostgreSQL geeft een nieuwe database `CONNECT` en `TEMPORARY` aan `PUBLIC`. Op de gedeelde
server van het platform staan de databases van alle projecten naast elkaar, naast die van
Keycloak, Forgejo, de mailrelay en OPI zelf, dus elke rol kon met elke database verbinden en
de catalogus ervan lezen. Dat is het probleem dat deze afsluiting wegneemt: least privilege
op databaseniveau (BIO2 8.03 en 8.22).

Na de afsluiting opent een rol alleen de databases waarvoor hij expliciet `CONNECT` heeft,
of waarvan hij de eigenaar is.

## Hoe het werkt

`PostgresConnector.restrict_database_to_roles(database, roles)` doet per database:

```sql
REVOKE CONNECT, TEMPORARY ON DATABASE "<db>" FROM PUBLIC;
GRANT CONNECT ON DATABASE "<db>" TO "<rol>";   -- per rol
```

Beide zijn in PostgreSQL idempotent, dus elke verwerking mag dit opnieuw doen. De eigenaar
houdt zijn rechten los van de GRANT-lijst: een REVOKE van `PUBLIC` raakt hem niet.

Vijf aanroepers:

* **Een projectdatabase**: `DatabaseManager.create_resources_for_deployment` roept de helper
  aan met de twee rollen die OPI voor die database provisioneert, de eigenaar
  (`{project}_{deployment}`) en de read-only rol (`..._ro`). Dat gebeurt bij elke
  verwerking, dus ook een database die al bestond wordt afgesloten. De aanroep staat niet in
  `create_database`, want daar zijn de rollen niet bekend.
* **Een nieuwe generatie bij een restore**: `opi/api/restore_router.py` sluit de verse
  generatiedatabase af met alleen de eigenaar.
* **Een kloon van een externe bron**: `DatabaseManager._execute_external_clone` doet hetzelfde
  voor de doeldatabase en voor de `_vN`-database die `force_clone` aanmaakt. De handmatige
  kloon-API komt daar langs buiten een verwerking om, dus zonder deze aanroep zou die
  database open blijven tot de volgende verwerking.
* **De platformdatabases**: `opi/runtime/startup/platform_database_access.py` loopt bij de start van OPI
  langs `keycloak`, `forgejo`, `mailrelay` en de eigen database van OPI. Die laatste krijgt
  `DATABASE_APP_USER` expliciet in zijn rollenlijst: OPI opent haar sinds RC-410 met een
  eigen rol en niet meer als superuser (`features/opi-op-een-eigen-databaserol.md`), en
  zonder die regel zou deze afsluiting OPI buiten zijn eigen database zetten. Dit kan niet
  declaratief: de `Database`-resource van CNPG beschrijft naam, eigenaar, encoding, locale,
  `allowConnections`, `connectionLimit`, schemas en extensies, en geen rechten per rol.
* **De bestaande projectdatabases bij de start van OPI**:
  `restrict_tenant_databases` in `opi/runtime/startup/platform_database_access.py` loopt na de
  platformronde langs elke projectdatabase op de gedeelde server. Zonder die ronde blijft een
  database open tot zijn project een keer verwerkt wordt, en dat kan weken duren. De rollen
  komen uit PostgreSQL zelf en niet uit de projectbestanden: de eigenaar (`pg_database.datdba`)
  houdt zijn rechten los van de GRANT, en `<eigenaar>_ro` krijgt `CONNECT` als die rol bestaat.
  Zo werkt het ook voor generaties (`_vN`), PR-databases, klonen en wezen. Overgeslagen worden
  de database `postgres`, de platformdatabases, de eigen database van OPI, een database die
  geen verbindingen toestaat en een database waarvan de eigenaar superuser is. Een fout op een
  database wordt gelogd en stopt de ronde niet; aan het eind staat er een regel met de
  tellingen per status. Op `dry-run` logt de ronde per database wie er nu verbonden is en wie
  de toegang zou verliezen, en dat is precies de meting die een beheerder voor het omzetten
  wil zien.

## Configuratie

| Instelling | Waarden | Standaard |
|---|---|---|
| `DATABASE_RESTRICT_PUBLIC_CONNECT` | `off`, `dry-run`, `on` | `on` |

* `off`: de helper raakt de server niet aan.
* `dry-run`: logt per database welke `REVOKE` en welke `GRANT` hij zou doen, welke rollen op
  dat moment verbonden zijn volgens `pg_stat_activity`, en welke daarvan hun toegang zouden
  verliezen. Er wordt niets uitgevoerd.
* `on`: voert de statements uit.

`on` is sinds RC-519 de standaard, dus elk cluster sluit zijn databases af zonder dat zijn
overlay iets zet. `dry-run` was de eerste stand, om dezelfde reden als bij
`RECONCILIATION_DRY_RUN`: de REVOKE raakt elke database op de server, en een rol die stil op
`PUBLIC` leunt valt dan om. Die stand blijft als bewuste opt-out per cluster, in de configmap
van de operations-manager:

```
DATABASE_RESTRICT_PUBLIC_CONNECT=dry-run
```

De sandbox noemt `on` expliciet in zijn configmap, want de meting daar leest die regel
(`tests/e2e/test_sandbox_database_isolatie.py`).

## Wat er nog meer verandert

* **De database-console valt niet meer terug op read-write.** Ontbreekt de read-only rol van
  een deployment (een deployment die nog niet is herverwerkt sinds die rol bestaat), dan
  opent de console niet en meldt hij dat een herverwerking de rol aanmaakt. Eerder verbond
  hij met de read-write gebruiker, met een WARNING in het log: een console die read-only
  heet en wel kan schrijven.
* **`postInitSQL` accepteert alleen een extensie.** Het veld draait als superuser van het
  eigen CNPG-cluster, dus een vrije SQL-regel is daar een superuser-shell. De vorm is
  `CREATE EXTENSION IF NOT EXISTS <naam> [SCHEMA <schema>];`, met de naam kaal of tussen
  dubbele aanhalingstekens (`"uuid-ossp"`). Een gequote naam draagt alleen letters, cijfers,
  `_`, `$` en een streepje: de regel komt ongequote onder `postInitApplicationSQL` in de
  `Cluster` terecht, dus een YAML-regelovergang of een dubbele punt in de naam zou daar een
  eigen sleutel worden. Een andere vorm geeft een validatiefout bij het opslaan en verwerken
  van het projectbestand.

## Niet afgesloten

* De database `postgres`. Dat is de onderhoudsdatabase, zonder data, en de weg waarlangs
  gereedschap de serverlijst opvraagt. Daar hangt ook de CNPG-pooler aan, zie hieronder.
* TLS afdwingen op de verbindingen: een eigen plan.
* `enableSuperuserAccess` vervangen door een provisioningrol met alleen `CREATEDB` en
  `CREATEROLE`: vraagt eerst een inventaris van welke handelingen van OPI superuser nodig
  hebben (extensies, `privileges`, klonen).

## De PgBouncer-pooler blijft werken

De pooler van `claude/pgbouncer-connection-pooling` (#116) komt op dezelfde gedeelde server
te staan en heeft geen plek nodig in de GRANT-lijst van een projectdatabase:

* PgBouncer authenticeert een client met `auth_user = cnpg_pooler_pgbouncer` en
  `auth_dbname = postgres`. De lookupfunctie `public.user_search` staat in de database
  `postgres`, en CNPG doet daar zelf `GRANT CONNECT ON DATABASE postgres TO
  cnpg_pooler_pgbouncer`. Die database sluit deze afsluiting niet af, dus het authenticatiepad
  blijft heel.
* De verbinding naar de doeldatabase legt PgBouncer als de rol van de client, niet als de
  poolerrol. Die rol heeft zijn eigen `CONNECT` uit de GRANT-lijst hierboven.

Stopt de pooler ooit met `auth_dbname = postgres` (dat vraagt een `user_search` per database,
de vorm van voor PgBouncer 1.19), dan moet `cnpg_pooler_pgbouncer` wel CONNECT krijgen op elke
gepoolde database. Dat is dan een rol extra in de lijst bij de aanroepers hierboven, niet een
nieuwe weg.

## Afhankelijkheden

* De gedeelde CNPG-cluster `rig-db` in `rig-system` (productie: `rig-prd-operations`).
* De read-only rol per deployment, zie `features/postgresql-scope-and-schemas.md` voor de
  schema's die bij zo'n database horen.
