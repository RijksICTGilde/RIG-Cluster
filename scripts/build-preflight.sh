#!/usr/bin/env bash
#
# build-preflight.sh - weiger een image-build te starten als er te weinig geheugen vrij is.
#
# Aanleiding: een build op de gedeelde dev-server trok de machine bijna om (load 34,8)
# terwijl er twee andere sessies draaiden. Dat was vooraf te zien. Deze controle kijkt
# naar het beschikbare geheugen en meldt wat er draait, zodat je weet wie je omver zou
# duwen.
#
# LET OP - MemFree is niet het getal dat je wilt. Het incidentverslag noemde "nog 1 GB
# van de 15 vrij"; dat was MemFree, en die staat op deze machine ALTIJD rond de 0,2-0,9
# GB omdat de paginacache (Cached, ~4 GB) hem opvult. Die cache is direct opvraagbaar.
# MemAvailable telt hem mee en is daarom het enige bruikbare signaal; dit script leest
# dus MemAvailable.
#
# De drempel is gemeten, niet geschat. Een koude build (alle drie de apt-rondes, uv
# sync, skopeo, tarball-export) piekt op 427 MB, een warme op 108 MB, en MemAvailable
# zakte tijdens de koude build van 4600 naar 4130 MB. 1536 MB is dus ruim drie keer wat
# een koude build kost. De eerdere 6144 kwam uit een schatting (de 4 GiB-cgroupgrens plus
# marge) en blokkeerde daardoor elke build op een machine die nooit boven de 5,5 GB komt.
#
# Geheugen is niet de schijf: bij dat incident was de schijf 10% vol.
#
# Omgevingsvariabelen:
#   BUILD_MIN_AVAILABLE_MB   minimaal vrij geheugen in MB (standaard 1536)
#   BUILD_PREFLIGHT_SKIP=1   controle overslaan (bewuste keuze, wordt gemeld)
#   BUILD_PREFLIGHT_MEMINFO  pad naar meminfo (standaard /proc/meminfo, voor tests)
#   BUILD_PREFLIGHT_LOADAVG  pad naar loadavg (standaard /proc/loadavg, voor tests)
#
# Exitcodes: 0 = ga je gang, 1 = te weinig vrij.

set -uo pipefail

MIN_AVAILABLE_MB="${BUILD_MIN_AVAILABLE_MB:-1536}"
MEMINFO="${BUILD_PREFLIGHT_MEMINFO:-/proc/meminfo}"
LOADAVG="${BUILD_PREFLIGHT_LOADAVG:-/proc/loadavg}"

if [ "${BUILD_PREFLIGHT_SKIP:-}" = "1" ]; then
    echo "[build-preflight] overgeslagen (BUILD_PREFLIGHT_SKIP=1)"
    exit 0
fi

if [ ! -r "$MEMINFO" ]; then
    echo "[build-preflight] kan $MEMINFO niet lezen, controle overgeslagen"
    exit 0
fi

available_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "$MEMINFO")"
if [ -z "${available_kb:-}" ]; then
    echo "[build-preflight] geen MemAvailable in $MEMINFO, controle overgeslagen"
    exit 0
fi
available_mb=$((available_kb / 1024))

load="onbekend"
if [ -r "$LOADAVG" ]; then
    load="$(awk '{print $1, $2, $3}' "$LOADAVG")"
fi

# Wat er draait: sessies, clusters en buildbakken. Zonder dit is "te weinig vrij" een
# getal zonder adres, en ga je alsnog gokken wie er in de weg zit.
running_processes() {
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        echo "  (geen docker-toegang, kan niet tonen wat er draait)"
        return
    fi
    docker ps --format '  {{.Names}} ({{.Image}})' 2>/dev/null |
        grep -Ei 'claude|kind|control-plane|worker|buildkit|postgres' |
        head -20 ||
        echo "  (niets herkenbaars)"
}

echo "[build-preflight] vrij geheugen: ${available_mb} MB (minimaal ${MIN_AVAILABLE_MB} MB), load: ${load}"

if [ "$available_mb" -lt "$MIN_AVAILABLE_MB" ]; then
    echo "[build-preflight] TE WEINIG VRIJ GEHEUGEN - de build start niet." >&2
    echo "[build-preflight] Nu draait er:" >&2
    running_processes >&2
    echo "[build-preflight] Wacht tot er ruimte is, of overleg met wie er bezig is." >&2
    echo "[build-preflight] Bewust toch doorgaan: BUILD_PREFLIGHT_SKIP=1 <commando>" >&2
    exit 1
fi

echo "[build-preflight] Nu draait er:"
running_processes
exit 0
