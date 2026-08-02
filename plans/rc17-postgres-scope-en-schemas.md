# Meerdere schema's en een scope-keuze voor de PostgreSQL-service

Status: ontwerpnotitie, 2 augustus 2026. Niet gebouwd. **Alle zes de openstaande beslissingen zijn genomen op 2 augustus; zie sectie 10. Waar de tekst hierboven nog een voorstel doet, wint sectie 10.** Aanleiding: een project kan vandaag precies één schema hebben, en de toegewijde database is een aparte service met een eigen namespace terwijl hij feitelijk gewoon een andere plaatsingskeuze is.

Doel van dit document: een plan dat gebouwd kan worden, met de open beslissingen expliciet, zonder het bestaande mechanisme voor generaties, klonen en backups te slopen.

Alle paden zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen.

---

## 1. Wat er nu staat, gemeten

Drie namen komen uit hetzelfde paar `(project, deployment)`, in `utils/naming.py`:

| Wat | Functie | Vorm |
|---|---|---|
| Database | `generate_database_name` (regel 539) | `{project}_{deployment}`, met `_v{generation}` bij een kloon |
| Schema | `generate_database_schema` (regel 520) | `{project}_{deployment}`, **zonder** generatie |
| Gebruiker | `generate_database_username` (regel 501) | `{project}_{deployment}` |

Eén database per deployment, één schema erin, één gebruiker. Vier dingen die daar omheen al bestaan en die het ontwerp hieronder dragen:

**Er is al een tweede rol met per-schema-rechten.** `_ensure_readonly_role` (`manager/database_manager.py:320`) maakt `{user}_ro`, doet `grant_readonly_on_schema(database, schema, user)` en zet een eigen `search_path`. Het mechanisme "meerdere rollen, elk met eigen grants op een schema" hoeft dus niet bedacht te worden, alleen gegeneraliseerd.

**De generatie zit in de databasenaam, niet in de schemanaam.** Een kloon maakt `proj_dep_v2` en alles wat daarin staat gaat mee. Dat is precies waarom extra schema's binnen dezelfde database het versiebeheer niet raken.

**Klonen is schema-scoped.** `pg_dump -n` in `connectors/postgres.py`, met een expliciete voorziening dat `-n` geen `CREATE EXTENSION` meeneemt en extensies daarom vooraf worden aangemaakt (regel 1262 en verder). Meer schema's betekent hier dus meer werk, want de kloon kent nu één schemanaam.

**De applicatie hoeft de schemanaam niet te kennen.** `set_role_search_path` (`database_manager.py:242`) zet het schema op de rol, dus een verbinding landt vanzelf in het goede schema. `DATABASE_SCHEMA` is een gemak, geen noodzaak. Dat is de belangrijkste vaststelling van dit document en sectie 4 leunt erop.

Wat er tegenwerkt: **het credential is per deployment, niet per component.** `build_secret_files` in `catalog/postgresql_database/__init__.py` maakt één `DatabaseSecret` voor de hele deployment en elk component krijgt hem. "Wie mag waarbij" vraagt dus per component een eigen rol en een eigen secret, en dat is de grootste ingreep in dit hele plan, groter dan de schema's zelf.

## 2. Scope als veld op de service, niet als aparte service

Vandaag zijn `postgresql-database` en `namespace-postgresql-database` twee services. Ze verschillen niet in wat ze de applicatie leveren maar in waar de database draait: gedeeld op een clusterinstantie, of een eigen CNPG-cluster in `rig-{project}-infrastructure` (`core/cluster_config.py:619`).

Dat is een plaatsingskeuze, geen andere dienst. Voorstel: één service `postgresql-database` met een veld `scope`:

| Waarde | Betekenis | Status |
|---|---|---|
| `shared` | Een database op de gedeelde clusterinstantie | Default; dit is wat `postgresql-database` nu doet |
| `project` | Eén eigen CNPG-cluster per project, gedeeld door alle deployments | Nu `namespace-postgresql-database` |
| `deployment` | Een eigen CNPG-cluster per deployment | Nieuw, niet in de eerste ronde |

`shared` is de default, dus een bestaand project dat niets zet verandert niet van gedrag. Dat is de hele reden om het zo te modelleren: de migratie is een verplaatsing van de bestaande `namespace-postgresql-database`-selectie naar `scope: project`, niet een herbouw.

**BESLIST (10.1):** het servicetype verdwijnt uiteindelijk, maar niet in de eerste ronde. `scope` komt erbij en `namespace-postgresql-database` blijft gewoon werken; het opruimen is een latere stap zodra de drie projecten die hem gebruiken (`algor-odc`, `mb-grist-helmfile`, `mb-docs-helmfile`) gemigreerd op schijf staan.

## 3. Meerdere schema's

Het bestaande schema blijft bestaan onder zijn huidige naam en wordt het default schema. Extra schema's krijgen `{project}_{deployment}_{postfix}`. Een zelfgekozen korte postfix; uniciteit via validator + enforcer; kort en streng (kleine letters, cijfers, underscore, begint met een letter, volledige naam onder 63 tekens). Valideer op de volledige naam, niet op de postfix alleen.

Versies en backups blijven werken zolang de schema's binnen dezelfde database blijven. De kloonweg gebruikt `pg_dump -n <schema>` met precies één schemanaam; dat wordt een lijst, en de extensie-voorbereiding moet per schema draaien.

## 4. Variabelen

**BESLIST (10.2):** per extra schema precies één variabele, `DATABASE_SCHEMA_{POSTFIX}`, plus de `APP_`-alias. Geen connectiestring per schema.
**BESLIST (10.3):** `DATABASE_SCHEMA` is het primaire schema van dát component (het eerste in zijn lijst); voor een component zonder eigen keuze is dat het default schema. De `search_path` begint daar ook.

## 5. Toegang per component (eigen ronde)

Eén rol per component, geen leesrecht-variant (BESLIST 10.4). De deployment-brede `_ro` blijft en krijgt SELECT op alle schema's; de database-console moet alle schema's kunnen tonen.

## 6. De UI

Een `Sequence` in de serviceconfigsectie, per item de postfix en een omschrijving. Toon de volledige naam en de variabele onder het veld. Verwijderen markeert i.p.v. weggooien (aparte bevestiging). Toegang per component hoort bij het component (options-provider).

**BESLIST (10.5):** schema's zijn strikt project-breed; elke deployment krijgt dezelfde schema's, elk in zijn eigen database.

## 7. Namespace van de toegewijde database

**BESLIST (10.6):** alles gaat naar de projectnamespace, ook de bestaande drie, maar als eigen traject met een terugvalplan (datamigratie op drie productiedatabases), **niet als onderdeel van dit plan**.

## 8. Volgorde

1. Scope-veld (shared/project) + migratie namespace-postgresql-database.
2. Meerdere schema's (secties 3 en 4), inclusief de kloonweg.
3. UI voor schema's (sectie 6, zonder het component-deel).
4. Toegang per component (sectie 5), eigen ronde.
5. Namespace-verhuizing (sectie 7), los van alles.
6. Scope `deployment`.

## 9. Invarianten

- Extra schema's leven in dezelfde database als het default schema.
- Een project dat niets nieuws opgeeft verandert niet.
- Naamgeving faalt luid (botsende naam, afkap-samenval, variabele-overschrijving) bij het opslaan.

## 10. Genomen beslissingen (2 augustus 2026)

1. `namespace-postgresql-database` blijft voorlopig bestaan; `scope` komt erbij.
2. Geen connectiestring per schema; alleen `DATABASE_SCHEMA_{POSTFIX}` + `APP_`-alias.
3. `DATABASE_SCHEMA` = primaire schema van het component (eerste in zijn lijst).
4. Eén rol per component, geen leesrecht-variant; de `_ro` krijgt SELECT op alle schema's.
5. Schema's zijn strikt project-breed.
6. Alle toegewijde databases naar de projectnamespace, als eigen traject met terugvalplan.
