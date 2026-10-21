# TLS naar de gedeelde PostgreSQL

## Wat het is

De gedeelde platformdatabase `rig-db` (CNPG, namespace `rig-system`) biedt TLS aan, maar
dwingt het niet af: zonder `hostssl`-regels in `pg_hba` mag een client ook onversleuteld
verbinden. Deze feature doet het werk dat vooraf gaat aan dat afdwingen:

1. een metriek die per rol en database telt hoeveel verbindingen zonder TLS lopen;
2. de platformdiensten die OPI beheert verbinden met TLS.

Het afdwingen zelf staat hieronder beschreven maar is niet aangezet. Dat is bewust: welke
tenant-app met welke driver en welke `sslmode` verbindt, weten we niet, en een
`hostssl`-regel weigert die apps zonder waarschuwing. Eerst meten, dan afdwingen.

Aanleiding: versleuteling in transit, BIO2 8.24 en 8.20.

## De metriek

`infrastructure/bootstrap/infrastructure/postgresql/database/base/monitoring-queries.yaml`
is een ConfigMap met een eigen monitoring-query, gekoppeld in `cluster.yaml` via
`monitoring.customQueriesConfigMap`. De standaardqueries van CNPG blijven ernaast staan.

```sql
SELECT a.usename AS usename,
       a.datname AS datname,
       count(*) AS count
FROM pg_stat_activity a
JOIN pg_stat_ssl s ON s.pid = a.pid
WHERE a.backend_type = 'client backend'
  AND a.client_addr IS NOT NULL
  AND NOT s.ssl
GROUP BY 1, 2
```

De metriek heet `cnpg_connections_without_tls_count` en heeft de labels `usename` en
`datname`. CNPG schrijft hem weg op zijn metrics-poort, en de PodMonitor in dezelfde map
levert hem aan Prometheus.

Drie dingen om te weten bij het lezen:

- **Afwezig betekent nul.** Geeft de query geen rijen, dan publiceert CNPG geen reeks. Een
  vraag als "welke rol verbindt zonder TLS" is dus `cnpg_connections_without_tls_count`
  zonder meer; een alert op nul moet `absent()` gebruiken.
- **Het is een momentopname.** `pg_stat_activity` toont de verbindingen die er nú zijn, dus
  een korte onversleutelde verbinding tussen twee scrapes blijft onzichtbaar. Voor de vraag
  "welke diensten verbinden structureel zonder TLS" is dat genoeg, want die houden hun pool
  open.
- **Een verbinding via een pooler telt hier als TLS**, ook als de app zelf onversleuteld met
  de pooler praat. De query meet één van de twee benen. Zie "Een pooler in de weg".

`client_addr IS NOT NULL` filtert de verbindingen over de unix-socket weg. Die hebben per
definitie geen TLS, en de instance-manager van CNPG gebruikt ze zelf; zonder dat filter
staat de teller nooit op nul.

### Zelf kijken

```bash
# In Prometheus of via de metrics-poort van de pod:
kubectl -n rig-system exec rig-db-1 -- curl -s localhost:9187/metrics \
  | grep connections_without_tls

# Of direct in de database:
kubectl -n rig-system exec -it rig-db-1 -- psql -c "
  SELECT a.usename, a.datname, s.ssl, s.version
  FROM pg_stat_activity a JOIN pg_stat_ssl s ON s.pid = a.pid
  WHERE a.backend_type = 'client backend' AND a.client_addr IS NOT NULL;"
```

## Wat met TLS verbindt

| Dienst | Waar | Hoe |
|---|---|---|
| Keycloak | `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` | `KC_DB_URL=jdbc:postgresql://rig-db-rw:5432/keycloak?sslmode=require` |
| OPI | `opi/core/config.py` | setting `DATABASE_SSLMODE`, standaard `require` |
| Mailrelay | `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml` | `tls.enable = true` op `[store.db]` |

Alle drie gebruiken `require` en niet `verify-full`: dat versleutelt zonder het
servercertificaat te toetsen. CNPG tekent met zijn eigen interne CA, en die zit in geen van
deze clients in de truststore. `verify-full` vraagt dat CA-certificaat in elke pod en is een
vervolgstap.

Bij Stalwart hoort `tls.allow-invalid-certs = true` bij `tls.enable = true` om dezelfde
reden: zonder die regel toetst hij tegen de publieke roots en weigert hij de verbinding.

### OPI

`DATABASE_SSLMODE` is één setting, en hij dekt deze verbindingen:

| Waar | Wat |
|---|---|
| `opi/connectors/postgres.py` | de `PostgresConnector`, de weg waarlangs OPI rollen, databases en schemas beheert |
| `opi/core/database_pools.py` | de startup-check die de eigen database aanmaakt, en de asyncpg-pool eromheen |
| `opi/worker_main.py` | de eigen pool van de taskworker |
| `opi/core/db.py`, `opi/migrations/env.py` | de SQLAlchemy-engine en Alembic |
| `opi/manager/database_manager.py` | de toets of de opgeslagen credentials van een deployment nog werken |

De waarden zijn die van libpq (`disable`, `allow`, `prefer`, `require`, `verify-ca`,
`verify-full`). Een lokale ontwikkeldatabase zonder TLS zet hem op `disable`;
`docker-compose.dev.yaml` doet dat voor de Postgres in die compose.

Let op het verschil in parameternaam bij de URL-vormen: asyncpg leest `?ssl=`, psycopg2
(Alembic) leest `?sslmode=`. De waarde is dezelfde.

De credentialtoets hoort hier met een randvoorwaarde. Zijn aanroeper leest "verbinding
mislukt" als "wachtwoord ongeldig" en zet het wachtwoord dan opnieuw, dus een mislukte
TLS-handshake mag daar niet in vallen. Hij vangt daarom alleen af wat de server bewust
weigert (`InvalidAuthorizationSpecificationError`) en wat normaal gebeurt voordat de
database bestaat (`InvalidCatalogNameError`); elke andere fout komt naar boven.

### Wat nog buiten de setting valt

Terecht erbuiten, want het is niet onze database: de validatie van een externe
bronverbinding bij een clone of migratie (`_validate_external_source` in
`database_manager.py`, en het externe pad van `_get_schema_extensions` in de connector). Die
server bepaalt zelf of hij TLS aanbiedt.

Openstaand, verbindingen van OPI naar een database van het platform die de setting nog niet
dragen:

- `opi/manager/db_console_manager.py` bouwt de `DATABASE_URL` voor pgweb en dbgate zonder
  `sslmode`. Dat is een sessie die blijft openstaan, dus hij is in de metriek ook echt
  zichtbaar.
- `opi/manager/project_manager.py` toetst met `_test_infrastructure_database_connection` of
  een dedicated CNPG-cluster al draait. Die clusters staan in deze feature buiten scope.
- `opi/utils/secrets.py` zet de connectiestring die OPI aan tenant-apps meegeeft. Die kiezen
  bewust zelf, zie "Wat een team met zo'n app moet doen" hieronder.

## Een pooler in de weg

Er loopt een tak die een CNPG-pooler (PgBouncer) voor `rig-db` zet, zodat projectworkloads
hun verbindingen bundelen: `claude/pgbouncer-connection-pooling`, issue #116, ontwerp in
`features/pgbouncer-connection-pooling.md`. Dan zijn er twee benen, en TLS is per been een
eigen vraag:

```
app  --(been 1: client naar pooler)-->  rig-db-pooler-rw  --(been 2: pooler naar server)-->  rig-db-rw
```

**Been 2 is al versleuteld en blijft dat na het afdwingen.** CNPG zet voor een pooler
standaard `server_tls_sslmode = verify-ca` met de server-CA van het cluster, dus de pooler
verbindt met TLS en toetst ook het certificaat. Zijn auth_query-verbinding valt onder de
vaste regel `hostssl all cnpg_pooler_pgbouncer all cert map=cnpg_pooler_pgbouncer`, en die
staat in de vaste sectie, dus boven de regels uit `pg_hba`. De serververbindingen die hij
namens een projectrol opzet matchen `hostssl all all all scram-sha-256`. Het afdwingen
hieronder sluit de pooler dus niet buiten. Eén voorwaarde: laat `server_tls_sslmode`
staan. CNPG laat die parameter in `spec.pgbouncer.parameters` overschrijven, dus zwakker
zetten kan, en dan valt de pooler na het afdwingen buiten.

**Been 1 is dat niet.** CNPG zet standaard `client_tls_sslmode = prefer`, dus PgBouncer
accepteert een client zonder TLS en versleutelt daarna zelf naar de server. Zo'n app staat
niet in `cnpg_connections_without_tls_count`: de meting ziet been 2, dat in orde is. Voor
apps achter een pooler is de metriek dus geen bewijs.

Been 1 heeft geen eigen metriek. De exporter van de pooler leest alleen `SHOW LISTS`,
`SHOW POOLS` en `SHOW STATS`, en daar zit geen TLS-kolom in. Meten gaat via de
admin-console van PgBouncer, en die hoort bij de pod: `admin_users = pgbouncer` plus de
regel `local pgbouncer pgbouncer peer` in zijn hba-bestand, dus alleen over de socket
binnen de pooler.

```bash
# De kolom tls is leeg bij een client zonder TLS.
kubectl -n rig-system exec deploy/rig-db-pooler -- \
  psql -h /controller/run -U pgbouncer pgbouncer -c "SHOW CLIENTS"
```

Afdwingen gaat met één parameter op de Pooler, en pas als die kolom nergens leeg meer is:

```yaml
spec:
  pgbouncer:
    parameters:
      client_tls_sslmode: "require"
```

PgBouncer weigert dan een client zonder TLS. Dat is geen `pg_hba`-werk: zijn hba-bestand
heeft net als dat van de server een vaste `host all all 0.0.0.0/0 md5` als laatste regel,
dus een `hostssl`-regel in `spec.pgbouncer.pg_hba` zou er een `hostnossl all all all reject`
naast nodig hebben. Die ene parameter doet hetzelfde en is minder om fout te doen.

Wat al klopt aan onze kant: `sslmode=require` werkt naar een pooler. CNPG geeft hem altijd
een servercertificaat (`client_tls_cert_file` staat in de parameters die CNPG vastzet), dus
de handshake lukt, en `prefer` aan de pooler laat hem toe. Verhuist de host in de
`DatabaseSecret` van een deployment naar de pooler, dan verbindt de credentialtoets van OPI
daar met `DATABASE_SSLMODE` zonder dat er iets aan hoeft. `verify-full` lukt ook daar niet,
om dezelfde reden als bij de server: het certificaat komt van de interne CA van CNPG.

## Afdwingen: de vervolgstap

De stap die hierna hoort, en die nog NIET is gezet. In `cluster.yaml`:

```yaml
spec:
  postgresql:
    pg_hba:
      - hostssl all all all scram-sha-256
      - hostnossl all all all reject
```

De tweede regel is nodig en geen gordel om de bretels. CNPG bouwt `pg_hba.conf` in vier
delen: zijn vaste regels, dan de regels uit `pg_hba` hierboven, dan de optionele
LDAP-sectie, en als laatste altijd zijn eigen `host all all all scram-sha-256`. Die
laatste is niet weg te halen, en `host` matcht ook een verbinding zonder TLS. Zet je
alleen de `hostssl`-regel, dan valt een onversleutelde verbinding door naar de
standaardregel en wordt hij alsnog toegelaten. De `hostnossl ... reject` ervoor sluit dat
pad af: zo'n client krijgt `pg_hba.conf rejects connection for host ...`.

De socket-regel (`local all all peer`) staat bij de vaste regels en blijft dus staan; de
instance-manager van CNPG verbindt daarover.

**De voorwaarde om dit aan te zetten:** `cnpg_connections_without_tls_count` is in
productie een week lang afwezig (dus nul) voor elke rol. Niet alleen voor de
platformdiensten: elke projectrol hangt er ook in, en die weigeren betekent een app die
omvalt. Staat er dan een pooler voor `rig-db`, dan komt de kolom `tls` uit `SHOW CLIENTS`
erbij, want voor die apps zegt de metriek niets over been 1.

Bij het schrijven van dit document stond Forgejo nog op `SSL_MODE=disable`
(`infrastructure/bootstrap/infrastructure/forgejo/controller/base/statefulset.yaml`,
de sandbox-overlay en `config/base/bootstrap-job.yaml`). Zolang dat zo is, haalt de rol
`forgejo` de voorwaarde niet.

### Wat een team met zo'n app moet doen

Het label `usename` in de metriek is de rol waarmee de app verbindt, en die rolnaam is
afgeleid van het project en de deployment. Staat jouw rol in de metriek, dan verbindt je app
onversleuteld en moet hij een `sslmode` meekrijgen voordat het afdwingen aan gaat:

- libpq, psycopg2, psql: `?sslmode=require` in de connectiestring, of `PGSSLMODE=require`;
- asyncpg: `ssl="require"` bij `connect`, of `?ssl=require` in de URL;
- JDBC: `?sslmode=require`;
- Go (pgx, lib/pq): `sslmode=require` in de DSN;
- Node (pg): `ssl: { rejectUnauthorized: false }`, want de interne CA is niet publiek
  vertrouwd.

`require` is wat het platform nu vraagt. Toets je het certificaat (`verify-full`), dan heb
je het CA-certificaat van CNPG nodig; dat is de vervolgstap hierboven.

## Buiten scope

- De `pg_hba`-regel aanzetten, zie de voorwaarde hierboven.
- De pooler van issue #116 zelf, die er nog niet is. Wat hij nodig heeft staat in "Een
  pooler in de weg".
- `verify-full` met het CA-certificaat van CNPG in de clients.
- De dedicated CNPG-clusters van projecten. `rig-db` is de gedeelde database; hetzelfde
  patroon kan daar later. De `PostgresConnector` van OPI verbindt ook naar die clusters al
  met `require`, want CNPG biedt TLS standaard aan.
- Rechten op databases en rollen, dat is een eigen traject.

## Afhankelijkheden

- CNPG 1.27 (`monitoring.customQueriesConfigMap`, `pg_hba`)
- PostgreSQL 17 (`pg_stat_ssl`)
- Prometheus via de PodMonitor `rig-db` in `rig-system`
