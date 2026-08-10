#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# Operations Manager Docker Entrypoint
#
# Handles database migrations and application startup with better control
# and error handling than relying on Python startup hooks.
#
# Usage: docker-entrypoint.sh [-h HOST] [-p PORT] [-l LOGLEVEL] [-s]
#   -h HOST        Bind host (default: 0.0.0.0)
#   -p PORT        Bind port (default: 8000)
#   -l LOGLEVEL    Uvicorn log level (default: info)
#   -s             Skip database migrations (for testing/debugging)
##############################################################################

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration defaults
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOGLEVEL="${LOGLEVEL:-info}"
SKIP_MIGRATIONS="${SKIP_MIGRATIONS:-false}"

# Parse command-line arguments
while getopts "h:p:l:s" opt; do
    case $opt in
        h)
            HOST="$OPTARG"
            ;;
        p)
            PORT="$OPTARG"
            ;;
        l)
            LOGLEVEL="$OPTARG"
            ;;
        s)
            SKIP_MIGRATIONS="true"
            ;;
        *)
            echo "Usage: $0 [-h host] [-p port] [-l loglevel] [-s]" >&2
            echo "  -h HOST        Bind host (default: 0.0.0.0)" >&2
            echo "  -p PORT        Bind port (default: 8000)" >&2
            echo "  -l LOGLEVEL    Uvicorn log level (default: info)" >&2
            echo "  -s             Skip database migrations" >&2
            exit 1
            ;;
    esac
done

# Banner
echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║        Operations Manager Docker Entrypoint                ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Log configuration
echo -e "${BLUE}Configuration:${NC}"
echo "  Host:            $HOST"
echo "  Port:            $PORT"
echo "  Log Level:       $LOGLEVEL"
echo "  Skip Migrations: $SKIP_MIGRATIONS"
echo ""

##############################################################################
# Database Migrations
##############################################################################

if [ "$SKIP_MIGRATIONS" = "false" ]; then
    echo -e "${BLUE}━━━ DATABASE MIGRATIONS ━━━${NC}"

    # Verify alembic.ini exists
    if [ ! -f "alembic.ini" ]; then
        echo -e "${RED}✗ FATAL: alembic.ini not found in working directory${NC}"
        echo "  Expected: $(pwd)/alembic.ini"
        exit 1
    fi

    # Verify opi/migrations exists
    if [ ! -d "opi/migrations" ]; then
        echo -e "${RED}✗ FATAL: opi/migrations directory not found${NC}"
        echo "  Expected: $(pwd)/opi/migrations"
        exit 1
    fi

    echo -e "${YELLOW}▶${NC} Running Alembic migrations..."
    echo ""

    # De uitvoer wordt meegelezen, want dit commando kan om twee heel verschillende
    # redenen falen. `alembic upgrade head` importeert de applicatie, dus alles wat bij
    # het opstarten misgaat -- een instelling die niet bestaat, een import die stukloopt --
    # komt hier naar buiten zonder dat de database er iets mee te maken heeft. Dit blok
    # riep vroeger onvoorwaardelijk "Database migration failed" en wees op DATABASE_HOST,
    # ook bij een configuratiefout. De echte melding stond er wel boven, maar de conclusie
    # eronder sprak hem tegen en die conclusie is wat mensen lezen.
    MIGRATION_LOG="$(mktemp)"
    EXIT_CODE=0
    alembic upgrade head 2>&1 | tee "$MIGRATION_LOG" || EXIT_CODE=$?

    if [ "$EXIT_CODE" -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✓ Database migrations completed successfully${NC}"
        rm -f "$MIGRATION_LOG"
    else
        echo ""
        if grep -q "extra_forbidden" "$MIGRATION_LOG"; then
            # Deze image kent een instelling niet die hij wel meekrijgt. In de praktijk
            # betekent dat: de configuratie is nieuwer dan het image (een oude `latest`
            # naast een verse checkout). De instellingen zelf noemen, want dat zegt
            # meteen welke kant het op moet.
            echo -e "${RED}✗ FATAL: de configuratie past niet bij deze image${NC}"
            echo ""
            echo "Deze image kent de volgende instellingen niet:"
            grep -B1 "extra_forbidden" "$MIGRATION_LOG" | grep -oE "^[a-z][a-z0-9_]*$" | sort -u | sed 's/^/  - /'
            echo ""
            echo "Dat wijst er meestal op dat de image ouder is dan de configuratie die"
            echo "hij meekrijgt. Bouw de image opnieuw uit deze broncode, of haal de"
            echo "onbekende instellingen uit de ConfigMap."
        elif grep -qE "ValidationError|pydantic" "$MIGRATION_LOG"; then
            echo -e "${RED}✗ FATAL: de configuratie is ongeldig${NC}"
            echo ""
            echo "De applicatie kwam niet door zijn eigen instellingencontrole. De fout"
            echo "staat hierboven; de database is hier niet bij betrokken."
        elif grep -qiE "could not connect|connection refused|password authentication|does not exist|timeout expired" "$MIGRATION_LOG"; then
            echo -e "${RED}✗ FATAL: de database is niet bereikbaar (afsluitcode: $EXIT_CODE)${NC}"
            echo ""
            echo "Controleer DATABASE_HOST, DATABASE_NAME, DATABASE_ADMIN_NAME en"
            echo "DATABASE_ADMIN_PASSWORD, en of de database al draait."
        else
            # Niets herkend, dus ook niets beweren. Een verkeerde oorzaak noemen kost
            # meer tijd dan er geen noemen.
            echo -e "${RED}✗ FATAL: migratie mislukt (afsluitcode: $EXIT_CODE)${NC}"
            echo ""
            echo "De fout staat hierboven. Geen bekend patroon herkend, dus er wordt"
            echo "hier geen oorzaak geraden."
        fi
        rm -f "$MIGRATION_LOG"
        exit "$EXIT_CODE"
    fi
    echo ""
else
    echo -e "${YELLOW}⊘ Database migrations skipped (SKIP_MIGRATIONS=true)${NC}"
    echo ""
fi

##############################################################################
# Application Startup
##############################################################################

echo -e "${BLUE}━━━ APPLICATION STARTUP ━━━${NC}"
echo -e "${YELLOW}▶${NC} Starting Operations Manager..."
echo ""

# Use exec to replace shell process with the application
# This ensures proper signal handling (SIGTERM for graceful shutdown)
exec python -m opi.server
