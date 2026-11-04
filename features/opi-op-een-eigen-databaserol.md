# OPI op een eigen databaserol

**Status**: geimplementeerd
**Taak**: RC-410

## Wat het is

OPI heeft zijn eigen database (`operations_manager`) voor taken, sessies, de audit en de
projectstore. Die database stond op dezelfde gedeelde PostgreSQL als elke tenantdatabase,
en OPI opende haar als de `postgres`-superuser. Een foute query of een injectie in OPI's
eigen tabellen draaide daarmee als superuser op alle databases van de server.

Sinds deze taak opent OPI zijn eigen database met de rol `operations_manager`: eigenaar van
de database en van alles in haar schema `public`, en zonder `SUPERUSER`, `CREATEDB` of
`CREATEROLE`. De admin-credentials blijven alleen in de paden die tenant- en
platformdatabases beheren.

## Welke rol waarvoor

| Pad | Rol |
|---|---|
| OPI's eigen database: hoofdpool, de SQLAlchemy-engine, de taakworker, de migraties | `DATABASE_APP_USER` |
| De database aanmaken als ze nog niet bestaat (`_ensure_database_exists`) | `DATABASE_ADMIN_NAME` |
| Tenantdatabases, rollen en schemas (`DatabaseManager`, klonen, restore, reconciliatie, de orphan-sweep) | `DATABASE_ADMIN_NAME` |
| `restrict_platform_databases` (REVOKE op `PUBLIC`) | `DATABASE_ADMIN_NAME` |

Wat dit NIET oplost, en dat hoort eerlijk op tafel: wie code uitvoert in de pod heeft nog
steeds het superuser-wachtwoord in zijn omgeving, want het tenantbeheer heeft het nodig. Een
rol met `CREATEROLE`/`CREATEDB` in plaats van de superuser voor dat beheer is een eigen
ontwerp: PostgreSQL 16 en later laat zo'n rol alleen de rollen beheren die hij zelf maakte,
en alle bestaande tenantrollen zijn door `postgres` gemaakt.

## Hoe het werkt

**De rol** is een managed role in
`infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml`, naast
`platform_backup`. CNPG maakt hem aan en leest zijn wachtwoord uit het secret
`operations-manager-db-credentials`. Dezelfde naam staat als `owner` op de `Database`
`operations-manager-db` in `databases.yaml`; CNPG past een gewijzigde owner op een bestaande
database toe met `ALTER DATABASE ... OWNER TO`, dus manifest en werkelijkheid blijven gelijk.

**Het secret** komt uit de template
`infrastructure/bootstrap/infrastructure/secrets/templates/operations-manager-db-secret.yaml`
(`kubernetes.io/basic-auth`, met `cnpg.io/reload`). De OPI-Deployment leest er
`DATABASE_APP_USER` en `DATABASE_APP_PASSWORD` uit, bewust NIET `optional`: bestaat het
secret niet, dan start de pod niet. Dat is luider en omkeerbaar, en beter dan stil
terugvallen op de superuser.

De rol staat in `base` en komt dus op elk cluster, dus het secret moet dat ook. Per cluster
komt het er anders:

| Cluster | Hoe het secret er komt |
|---|---|
| `odcn` | Het SOPS-bestand staat in `secrets/config/overlays/odcn`, met een regel in `decrypt-sops.yaml` |
| `sandboxed-local` | `task sandbox:generate-infrastructure-secrets` maakt het bij de setup uit de template, met de sandbox-AGE-sleutel en `FIXED_PASSWORD`; die overlay staat in `.gitignore`. Een bestaand geheim slaat die taak per bestand over, dus op een draaiend cluster komt een nieuwe template er gewoon bij |
| `odcn-staging` | Handstap: staging leest zijn infrastructuursecrets uit de private repo `zad-staging-secrets`, niet uit deze repo |

**De pools.** Een OPI-pod opent maximaal 10 verbindingen naar zijn eigen database: de
asyncpg-hoofdpool (`MAIN_POOL_MAX_SIZE` 5 in `opi/core/database_pools.py`) en de
SQLAlchemy-engine (`ORM_POOL_SIZE` 5, `ORM_MAX_OVERFLOW` 0 in `opi/core/db.py`). Daarop is
de `connectionLimit` van de managed role gerekend: twee pods tijdens een uitrol plus de
verbinding van Alembic, met marge, is 25. Een pool verhogen betekent die limiet meeverhogen;
het commentaar bij de rol in `cluster.yaml` rekent de som voor.

**Het eigendom van bestaande objecten** verhuist met
`python -m opi.core.app_database_owner`, die de entrypoint draait vóór
`alembic upgrade head`. Die stap gebruikt de admin-credentials, en verzet de database en
elke tabel, sequence, view, materialized view, type en functie in `public` die nog van
`DATABASE_ADMIN_NAME` is, inclusief `alembic_version`. Hij is idempotent: wat al goed staat
levert geen statement op. Geen `REASSIGN OWNED BY postgres`, want dat raakt ook objecten
buiten deze database en is op een superuser ongeldig.

Het schema `public` zelf blijft doorgaans van `pg_database_owner` (de standaard sinds
PostgreSQL 15), en volgt dus automatisch met de eigenaar van de database.

**De opstartcontrole** staat in `opi/core/database_pools.py`. Na de hoofdpool leest OPI
`rolsuper`, `rolcreaterole` en `rolcreatedb` van `current_user`. Is een van de drie waar, of
is `current_user` gelijk aan `DATABASE_ADMIN_NAME`, dan weigert OPI te starten. In een
ontwikkelomgeving (`local`, `sandboxed-local`) is het een WARNING, zodat
`docker-compose.dev.yaml` met de admin-gebruiker als app-gebruiker kan blijven draaien.

## Configuratie

| Instelling | Default | Waarvoor |
|---|---|---|
| `DATABASE_APP_USER` | `operations_manager` | De rol waarmee OPI zijn eigen database opent |
| `DATABASE_APP_PASSWORD` | geen | Het wachtwoord van die rol; leeg is een fout, geen terugval |

`DATABASE_APP_PASSWORD` staat in de denylist van `opi/utils/subprocess_env.py`, dus een
subproces dat OPI start erft hem niet.

## Uitrolvolgorde

1. **Infra eerst.** ArgoCD synct de managed role, het secret en de nieuwe `owner`. Controleer
   met read-only `kubectl` dat `operations-manager-db-credentials` bestaat en dat de rol in
   `pg_roles` staat.
2. **Daarna de OPI-release.** Komt OPI eerder, dan start de pod niet op het ontbrekende
   secret. Dat is luid en omkeerbaar.
3. **Staging is een handstap, en die gaat VOOR de OPI-release op staging.** Staging leest
   zijn infrastructuursecrets uit de private repo `zad-staging-secrets`, niet uit deze repo,
   dus daar moet `operations-manager-db-credentials` met de hand bij. De worker kan dat niet.
   Staat het secret er niet, dan start de OPI-pod op staging niet.

## Roteren

`scripts/secret_rotate.py` heeft de rij `operations-manager-db` (categorie `cnpg-secret`):
het bestand gaat eerst, de CNPG-operator past de rol dan zelf aan, en OPI herstart omdat hij
het wachtwoord als env leest. Zie `docs/operations/wachtwoorden-roteren.md`.

## Afhankelijkheden

* CNPG managed roles (`cluster.yaml`) en de `Database`-resource (`databases.yaml`)
* SOPS + AGE voor het secret per cluster
* `features/database-alleen-eigen-rollen.md`: de app-rol staat in de CONNECT-lijst van zijn
  eigen database, anders sluit OPI zich daar buiten zodra
  `DATABASE_RESTRICT_PUBLIC_CONNECT` op `on` staat
