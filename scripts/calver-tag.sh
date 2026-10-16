#!/usr/bin/env bash
#
# calver-tag.sh - de CalVer-tag van een image: {YYYY}.{MM}.{DD}.{HHMM}-{commit}[-dirty].
#
# "Vuil" gaat over de BRONPADEN van dat ene image en niet over de hele repo. Over de hele
# repo was de tag altijd -dirty, omdat publish-operations-manager zijn secrets-bestand in
# bootstrap/ laat herschrijven; dan zegt -dirty niets meer, en weigert de guard in de
# promote-taak een image dat in orde is. scripts/bron-tag.sh doet die meting al voor
# SANDBOX_OM_TAG, dus hij doet hem hier ook: een vuilheidscontrole, een lezer.
#
# Gebruik: scripts/calver-tag.sh <pad>...   (de paden waaruit de image wordt gebouwd)
#
# Exitcodes: 0 = klaar, 2 = fout gebruik.

set -euo pipefail

[ $# -gt 0 ] || { echo "[calver-tag] geef minstens een pad mee" >&2; exit 2; }

DIRTY=""
case "$("$(dirname "$0")/bron-tag.sh" "$@")" in
    *-dirty-*) DIRTY="-dirty" ;;
esac
printf '%s-%s%s\n' "$(date +%Y.%m.%d.%H%M)" "$(git rev-parse --short=8 HEAD)" "$DIRTY"
