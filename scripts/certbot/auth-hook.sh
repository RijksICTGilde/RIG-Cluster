#!/usr/bin/env bash
# certbot --manual-auth-hook
#
# Called by certbot with CERTBOT_DOMAIN / CERTBOT_VALIDATION in env.
# Sets the _acme-challenge TXT record in TransIP by exec'ing transip_set_dns.py
# inside the operations-manager pod (IP-whitelisted source).
#
# Requires (exported by parent renew-sandbox-cert.sh):
#   OPI_POD        operations-manager pod name
#   OPI_NAMESPACE  pod namespace (e.g. rig-prd-operations)
#   TRANSIP_ZONE   zone label, e.g. rijksapp.dev
set -euo pipefail

: "${OPI_POD:?OPI_POD not exported by parent}"
: "${OPI_NAMESPACE:?OPI_NAMESPACE not exported by parent}"
: "${TRANSIP_ZONE:?TRANSIP_ZONE not exported by parent}"
: "${CERTBOT_DOMAIN:?CERTBOT_DOMAIN not set (this script must be invoked by certbot)}"
: "${CERTBOT_VALIDATION:?CERTBOT_VALIDATION not set}"

# Derive TXT record name within the zone.
# CERTBOT_DOMAIN for *.sandbox.rijksapp.dev is "sandbox.rijksapp.dev"
# Zone is "rijksapp.dev", so prefix is "sandbox" → record name "_acme-challenge.sandbox"
PREFIX="${CERTBOT_DOMAIN%."$TRANSIP_ZONE"}"
if [[ "$PREFIX" == "$CERTBOT_DOMAIN" ]]; then
  echo "[auth-hook] ERROR: CERTBOT_DOMAIN=$CERTBOT_DOMAIN is not within zone $TRANSIP_ZONE" >&2
  exit 1
fi
if [[ -z "$PREFIX" ]]; then
  RECORD_NAME="_acme-challenge"
else
  RECORD_NAME="_acme-challenge.${PREFIX}"
fi

echo "[auth-hook] zone=$TRANSIP_ZONE name=$RECORD_NAME value=$CERTBOT_VALIDATION" >&2

kubectl -n "$OPI_NAMESPACE" exec "$OPI_POD" -- bash -c '
set -e
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
SECRET_JSON=$(curl -sS --cacert "$CA" -H "Authorization: Bearer $TOKEN" \
  https://kubernetes.default.svc/api/v1/namespaces/'"$OPI_NAMESPACE"'/secrets/transip-credentials)
export TRANSIP_ACCOUNT_NAME=$(echo "$SECRET_JSON" | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)[\"data\"][\"TRANSIP_ACCOUNT_NAME\"]).decode())")
export TRANSIP_PRIVATE_KEY=$(echo "$SECRET_JSON" | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)[\"data\"][\"TRANSIP_PRIVATE_KEY\"]).decode())")
python3 /tmp/transip_set_dns.py \
  --zone '"$TRANSIP_ZONE"' \
  --name '"$RECORD_NAME"' \
  --type TXT \
  --content '"'$CERTBOT_VALIDATION'"' \
  --replace
' >&2

# Wait for DNS propagation against an authoritative-ish public resolver.
echo "[auth-hook] Waiting for DNS to propagate (querying 8.8.8.8)..." >&2
FQDN="_acme-challenge.${CERTBOT_DOMAIN}"
for i in {1..60}; do
  RESULT=$(dig +short TXT "$FQDN" @8.8.8.8 2>/dev/null | tr -d '"' | head -1 || true)
  if [[ "$RESULT" == "$CERTBOT_VALIDATION" ]]; then
    echo "[auth-hook] DNS propagated after ${i} polls" >&2
    # Small grace period — Let's Encrypt queries multiple resolvers.
    sleep 5
    exit 0
  fi
  sleep 5
done
echo "[auth-hook] WARN: DNS did not propagate within 5 minutes; continuing anyway (Let's Encrypt may still succeed or retry)" >&2
