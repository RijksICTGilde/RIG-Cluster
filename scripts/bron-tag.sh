#!/usr/bin/env bash
#
# bron-tag.sh - schrijft de image-tag die bij de huidige bron hoort: de korte commit, en
# bij ongecommit werk in die paden een `-dirty-<hash>` erachter.
#
# De hash gaat over de INHOUD, ook van ongetrackte bestanden, want docker neemt die op in
# de buildcontext. `-c core.quotePath=false` houdt een naam met unicode of een spatie
# leesbaar voor `cat`.
#
# Gebruik: scripts/bron-tag.sh <pad>...   (de paden waaruit de image wordt gebouwd)
#
# Exitcodes: 0 = klaar, 2 = fout gebruik.

set -euo pipefail

[ $# -gt 0 ] || { echo "[bron-tag] geef minstens een pad mee" >&2; exit 2; }

tag="$(git rev-parse --short HEAD)"
if [ -n "$(git status --porcelain -- "$@")" ]; then
    tag="$tag-dirty-$( {
        git diff HEAD -- "$@"
        git -c core.quotePath=false ls-files -o --exclude-standard -- "$@" | while IFS= read -r f; do
            echo "$f"
            # Een vindplaats die geen leesbaar gewoon bestand is (een symlink naar een map
            # zoals een gesymlinkte .venv, een kapotte symlink) zou hier onder set -e de tag
            # doden, en daarmee elke taak: SANDBOX_OM_TAG is een globale Taskfile-var.
            cat "$f" 2>/dev/null || true
        done
    } | sha1sum | cut -c1-7)"
fi
echo "$tag"
