#!/usr/bin/env bash
#
# prune-kind-registry.sh - verwijdert oude tags uit de registry naast het kind-cluster en
# ruimt daarna de lagen op waar geen manifest meer naar verwijst.
#
# Wat blijft staan en waarom dit in het deploy-pad draait: docs/sandbox-kind-registry.md.
#
# Omgevingsvariabelen:
#   RETENTIE_DAGEN       tags met een image ouder dan dit aantal dagen gaan weg (standaard 2)
#   KIND_REGISTRY_NAME   containernaam (standaard kind-registry)
#   KIND_REGISTRY_PORT   hostpoort op 127.0.0.1 (standaard 5001)
#
# Exitcodes: 0 = klaar, 2 = fout gebruik, 3 = de registry draait niet,
# 4 = de registry staat geen deletes toe. Anders: de code van het mislukte commando.

set -euo pipefail

RETENTIE_DAGEN="${RETENTIE_DAGEN:-2}"
REG_NAME="${KIND_REGISTRY_NAME:-kind-registry}"
REG_PORT="${KIND_REGISTRY_PORT:-5001}"
REG_URL="http://127.0.0.1:${REG_PORT}"
ACCEPT="application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"

if [ $# -gt 0 ]; then
    echo "[registry-prune] onbekende optie: $1 (retentie gaat via RETENTIE_DAGEN)" >&2
    exit 2
fi
if ! [[ "$RETENTIE_DAGEN" =~ ^[0-9]+$ ]]; then
    echo "[registry-prune] RETENTIE_DAGEN moet een heel aantal dagen zijn, niet '$RETENTIE_DAGEN'" >&2
    exit 2
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != "true" ]; then
    echo "[registry-prune] registry $REG_NAME draait niet; zet hem neer met task sandbox:setup-registry" >&2
    exit 3
fi
if ! docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$REG_NAME" |
    grep -qx 'REGISTRY_STORAGE_DELETE_ENABLED=true'; then
    echo "[registry-prune] registry $REG_NAME staat geen deletes toe." >&2
    echo "[registry-prune] task sandbox:setup-registry maakt hem opnieuw aan, met behoud van de lagen." >&2
    exit 4
fi

cutoff=$(($(date +%s) - RETENTIE_DAGEN * 86400))

manifest() { curl -fsS -H "Accept: $ACCEPT" "$REG_URL/v2/$1/manifests/$2"; }

# Een index heeft geen config; dan telt het eerste image erin dat geen attestation is.
created_of() {
    local repo="$1" body="$2" config
    config="$(jq -r '.config.digest // empty' <<<"$body")"
    if [ -z "$config" ]; then
        local child
        child="$(jq -r '[.manifests[] | select(.platform.os != "unknown")][0].digest // empty' <<<"$body")"
        [ -n "$child" ] || return 0
        config="$(manifest "$repo" "$child" | jq -r '.config.digest // empty')"
    fi
    [ -n "$config" ] || return 0
    curl -fsS "$REG_URL/v2/$repo/blobs/$config" | jq -r '.created // empty'
}

size() { docker exec "$REG_NAME" du -sh /var/lib/registry | cut -f1; }

echo "[registry-prune] $REG_NAME: tags ouder dan $RETENTIE_DAGEN dag(en) weg, omvang voor: $(size)"

repos="$(curl -fsS "$REG_URL/v2/_catalog?n=1000" | jq -r '.repositories // [] | .[]')"
# garbage-collect faalt op een registry waar nooit naar gepusht is.
[ -n "$repos" ] || { echo "[registry-prune] klaar, de registry is leeg"; exit 0; }
for repo in $repos; do
    tags="$(curl -fsS "$REG_URL/v2/$repo/tags/list" | jq -r '.tags // [] | .[]')"
    # Per tag: moment, digest, en de digests eronder (bij een index).
    rows=""
    for tag in $tags; do
        headers="$(mktemp)"
        body="$(curl -fsS -D "$headers" -H "Accept: $ACCEPT" "$REG_URL/v2/$repo/manifests/$tag")"
        digest="$(tr -d '\r' <"$headers" | awk 'tolower($1) == "docker-content-digest:" {print $2}')"
        rm -f "$headers"
        stamp="$(created_of "$repo" "$body")"
        if [ -z "$stamp" ] || ! epoch="$(date -d "$stamp" +%s 2>/dev/null)"; then
            echo "[registry-prune] $repo:$tag heeft geen leesbaar bouwmoment, blijft staan"
            epoch=""
        fi
        children="$(jq -r '[.manifests[]?.digest] | join(" ")' <<<"$body")"
        rows+="${epoch:-keep} $tag $digest $children"$'\n'
    done
    [ -n "$rows" ] || continue

    newest="$(awk '$1 != "keep" {print $1}' <<<"$rows" | sort -n | tail -n1)"
    drop=""
    while read -r epoch tag digest children; do
        [ -n "$tag" ] || continue
        if [ "$epoch" = "keep" ] || [ "$epoch" -ge "$cutoff" ] || [ "$epoch" = "$newest" ]; then
            echo "[registry-prune] bewaard: $repo:$tag"
        else
            drop+="$digest $children "
            echo "[registry-prune] weg:     $repo:$tag ($(date -u -d "@$epoch" +%Y-%m-%dT%H:%MZ))"
        fi
    done <<<"$rows"

    # Een index eerst, dan zijn kinderen: de garbage collect laat een kind zonder tag
    # anders staan. Twee tags op dezelfde digest delen hun bouwmoment, dus wat hier weggaat
    # hangt nooit onder een bewaarde tag.
    for digest in $(tr ' ' '\n' <<<"$drop" | awk 'NF && !seen[$0]++'); do
        curl -fsS -o /dev/null -X DELETE "$REG_URL/v2/$repo/manifests/$digest"
    done
done

gc_log="$(docker exec "$REG_NAME" registry garbage-collect /etc/docker/registry/config.yml 2>&1)" || {
    rc=$?
    echo "$gc_log" >&2
    exit "$rc"
}
echo "[registry-prune] klaar, $(grep -c 'Deleting blob' <<<"$gc_log" || true) blobs opgeruimd, omvang na: $(size)"
