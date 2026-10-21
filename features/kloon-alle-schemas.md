# Een kloon neemt alle schema's van de brondatabase mee

`clone-from: deployment` kopieert de database van een andere deployment. Sinds RC-360 gaat
daarbij **elk** schema van de brondatabase mee, ook de schema's die de applicatie zelf
aanmaakte. Daarvoor kwamen alleen het standaardschema en de door ZAD gedeclareerde extra
schema's (`features/postgresql-scope-and-schemas.md`) mee.

## Waarom

Een project dat zijn tabellen in eigen schema's zet (OpenFSC gebruikt `peers` en
`contracts`) kreeg een preview waarin `schema_migrations` wel de stand van de bron had,
maar de tabellen niet bestonden. De migratietool zag werk dat al gedaan leek en de
applicatie viel om op `relation "peers.peers" does not exist`. Een database die over
zichzelf liegt is erger dan een lege.

`clone-from: backup` deed dit al goed: de back-uppod dumpt de hele database zonder `-n`.
Na RC-360 doen beide wegen hetzelfde.

## Welke schema's mee gaan

| Soort | Naam in het doel |
|---|---|
| Het standaardschema (`{project}_{bron}`) | hernoemd naar `{project}_{deployment}` |
| De extra schema's van ZAD (RC-17) | hernoemd naar `{project}_{deployment}_{postfix}` |
| Elk ander schema in de brondatabase | dezelfde naam |

Elke deployment heeft een eigen database, dus alles wat in de brondatabase staat hoort bij
de brondeployment. Er is geen veld om dit aan of uit te zetten: een halve kloon wil niemand.

Niet mee gaan:

- **`public`.** Een verse database heeft al een `public`, en `postInitSQL` kan daar
  extensies in gezet hebben voordat de kloon draait. Erover klonen zou dat schema met
  CASCADE droppen. Staan er in de bron objecten in `public` die niet bij een extensie
  horen (tabellen, sequences, views, functies, types), dan gaat de kloon door, maar
  noemt een waarschuwing in de log en in de taakvoortgang die objecten bij naam.
- **Het schema waar een extensie in woont** (bijvoorbeeld `topology` van
  `postgis_topology`). `postInitSQL` maakt dat in het doel al aan, en een kloon eroverheen
  zou weigeren. Dit geeft een INFO-regel.
- **Schema's die met `pg_` beginnen**, en `information_schema`.

## Welke namen het platform accepteert

Een naam die de applicatie koos komt terecht in SQL die OPI met de adminrol uitvoert
(`ALTER SCHEMA ... OWNER TO`, `DROP SCHEMA IF EXISTS ...`). Die statements quoten niet, dus
een naam moet voldoen aan `^[a-z_][a-z0-9_]*$` (`POSTGRES_CLONEABLE_SCHEMA`), niet langer
zijn dan 63 tekens en geen gereserveerd woord zijn. Strenger dan `POSTGRES_IDENTIFIER`:
geen hoofdletters en geen `$`, want ongequot leest PostgreSQL `Peers` als `peers`.

Voldoet een naam niet, of valt hij samen met een doelnaam van deze deployment zelf (een
schema `mft_tp9_pr_73` in de testdatabase), dan **faalt de kloon**, met de naam in de
melding en voordat er iets in de doeldatabase is aangemaakt. Geen stille skip: dat zou de
halve kloon opleveren die dit juist oplost.

## Eén dump voor alle schema's

Alle schema's gaan door **één** `pg_dump` met een `-n` per schema, in plaats van een run
per schema. pg_dump zet de objecten over de schema's heen zelf in de goede volgorde en
voegt foreign keys pas aan het eind toe, dus twee schema's die naar elkaar verwijzen
herstellen onder `ON_ERROR_STOP=1`. Voor zo'n cyclus bestaat geen volgorde waarin een run
per schema slaagt; dat raakte de extra schema's van RC-17 vandaag al.

De restore draait als de doelrol, dus de schema's en hun tabellen zijn eigendom van die
rol en de applicatie kan er met haar eigen rol in schrijven en migreren. Extensies worden
per schema vooraf in het doel aangemaakt, zoals eerder, want `pg_dump -n` laat
`CREATE EXTENSION` weg.

De ontdekte schema's komen bij `extra_clone_pairs` in `DatabaseManager`, dus de opruiming
na een mislukte kloon en de hervatlogica (`features/kloonpoging.md`) nemen ze vanzelf mee.
Voor de vraag hoe ver een afgebroken kloon kwam tellen ze niet mee: bron- en doelnaam zijn
gelijk, dus ze staan al onder hun doelnaam zodra pg_dump ze aanmaakt. Die staat leest de
hervatlogica af aan de schema's die hernoemen, en een gelijknamig schema dat nog in het doel
staat ruimt ze op als kloonpuin.

## Wat buiten deze wijziging valt

- **Een kloon uit een externe bron** (`remote-source`, `clone_schema_from_external`): daar
  is de bron een andere database en is "alle schema's" een andere vraag. Dat pad kloont nog
  één schema.
- **Read-only grants op de schema's van de applicatie.** `_ensure_readonly_user` geeft
  SELECT op het standaardschema en de ZAD-extra schema's. Schema's die de app zelf maakt
  vallen daarbuiten, met of zonder kloon.

## Waar het zit

| Onderdeel | Bestand |
|---|---|
| Inventarisatie van de bron | `opi/connectors/postgres.py` (`describe_schemas`, `SchemaInventory`) |
| Naamguard | `opi/connectors/postgres.py` (`validate_cloneable_schema_name`), `opi/utils/patterns.py` |
| Ontdekken en melden | `opi/manager/database_manager.py` (`_resolve_application_schemas`) |
| De dump | `opi/connectors/postgres.py` (`clone_schema`, `_execute_pgdump_clone`) |
| Tests | `tests/test_multi_schema.py`, `tests/test_clone_all_schemas.py` (`requires_infra`) |

De `requires_infra`-tests meten dit tegen een echte PostgreSQL 17, inclusief foreign keys
in beide richtingen tussen twee schema's van de applicatie, een view eroverheen, het
eigendom van de doelrol en beide faalpaden. Hoe je ze aanwijst staat bovenaan
`tests/test_clone_all_schemas.py`.
