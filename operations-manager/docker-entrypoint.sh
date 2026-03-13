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

    # Run migrations with detailed output
    if alembic upgrade head; then
        echo ""
        echo -e "${GREEN}✓ Database migrations completed successfully${NC}"
    else
        EXIT_CODE=$?
        echo ""
        echo -e "${RED}✗ FATAL: Database migration failed (exit code: $EXIT_CODE)${NC}"
        echo ""
        echo "Possible causes:"
        echo "  - Database is not available or not ready"
        echo "  - Missing database credentials in environment variables"
        echo "  - Invalid database configuration"
        echo "  - Schema conflicts or migration errors"
        echo ""
        echo "Debug: Check DATABASE_HOST, DATABASE_NAME, DATABASE_ADMIN_NAME, DATABASE_ADMIN_PASSWORD"
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
