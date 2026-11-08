#!/usr/bin/env bash
# Start de PostgreSQL-image van het platform als losse server.
#
# De CNPG-image heeft geen ENTRYPOINT en geen docker-entrypoint.sh (Cmd bash, user 26):
# CNPG start hem zelf met /controller/manager. Een "docker run -e POSTGRES_PASSWORD=..."
# start daarmee alleen bash, die direct eindigt. Dit script doet wat de operator doet en
# leest dezelfde env als de docker-library-image: POSTGRES_PASSWORD (verplicht),
# POSTGRES_USER, POSTGRES_DB, en POSTGRES_INIT_SQL voor de postInitApplicationSQL.
set -euo pipefail

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is verplicht}"
export PGDATA="${PGDATA:-/var/lib/postgresql/zad}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    install -d -m 700 "$PGDATA"
    pwfile=$(mktemp)
    printf '%s' "$POSTGRES_PASSWORD" >"$pwfile"
    # UTF8 met locale C, zoals CNPG zijn initdb draait. Zonder --encoding volgt initdb de
    # lege locale van de image en wordt alles SQL_ASCII, en daarin weigert JSONB elke
    # niet-ASCII-tekst (de voortgang van een taak met een "…" erin).
    initdb --username=postgres --pwfile="$pwfile" --auth-host=scram-sha-256 --encoding=UTF8 --locale=C >/dev/null
    rm -f "$pwfile"
    # Docker mapt de poort via het bridge-netwerk, dus een client komt niet van 127.0.0.1
    # en valt buiten de regels die initdb schrijft.
    echo "host all all all scram-sha-256" >>"$PGDATA/pg_hba.conf"

    # Alleen op een eigen socket in PGDATA: over TCP bestaat deze server niet en over de
    # gewone socket ook niet, zodat niets buiten deze init-stappen hem kan vinden.
    export PGHOST="$PGDATA"
    pg_ctl --wait --options="-c listen_addresses='' -c unix_socket_directories=$PGDATA" start >/dev/null
    rol="${POSTGRES_USER:-postgres}"
    if [ "$rol" != postgres ]; then
        createuser --superuser "$rol"
        # Via stdin met -v: psql interpoleert een variabele niet in een -c-argument.
        psql --quiet --no-psqlrc -v rol="$rol" -v pw="$POSTGRES_PASSWORD" <<'SQL'
ALTER ROLE :"rol" PASSWORD :'pw';
SQL
    fi
    db="${POSTGRES_DB:-$rol}"
    if [ "$db" != postgres ]; then
        createdb --owner="$rol" "$db"
    fi
    if [ -n "${POSTGRES_INIT_SQL:-}" ]; then
        psql --quiet --no-psqlrc --dbname="$db" -c "$POSTGRES_INIT_SQL"
    fi
    pg_ctl --wait --mode=fast stop >/dev/null
fi

exec postgres -c listen_addresses='*'
