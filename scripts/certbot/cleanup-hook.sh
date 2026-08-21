#!/usr/bin/env bash
# certbot --manual-cleanup-hook
#
# Removes the _acme-challenge TXT record after validation.
# Same env requirements as auth-hook.sh.
set -euo pipefail

: "${OPI_POD:?OPI_POD not exported by parent}"
: "${OPI_NAMESPACE:?OPI_NAMESPACE not exported by parent}"
: "${TRANSIP_ZONE:?TRANSIP_ZONE not exported by parent}"
: "${CERTBOT_DOMAIN:?CERTBOT_DOMAIN not set (this script must be invoked by certbot)}"

PREFIX="${CERTBOT_DOMAIN%."$TRANSIP_ZONE"}"
if [[ "$PREFIX" == "$CERTBOT_DOMAIN" ]]; then
  echo "[cleanup-hook] ERROR: CERTBOT_DOMAIN=$CERTBOT_DOMAIN is not within zone $TRANSIP_ZONE" >&2
  exit 1
fi
if [[ -z "$PREFIX" ]]; then
  RECORD_NAME="_acme-challenge"
else
  RECORD_NAME="_acme-challenge.${PREFIX}"
fi

echo "[cleanup-hook] Deleting TXT records: zone=$TRANSIP_ZONE name=$RECORD_NAME" >&2

kubectl -n "$OPI_NAMESPACE" exec "$OPI_POD" -- bash -c '
set -e
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
SECRET_JSON=$(curl -sS --cacert "$CA" -H "Authorization: Bearer $TOKEN" \
  https://kubernetes.default.svc/api/v1/namespaces/'"$OPI_NAMESPACE"'/secrets/transip-credentials)
export TRANSIP_ACCOUNT_NAME=$(echo "$SECRET_JSON" | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)[\"data\"][\"TRANSIP_ACCOUNT_NAME\"]).decode())")
export TRANSIP_PRIVATE_KEY=$(echo "$SECRET_JSON" | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)[\"data\"][\"TRANSIP_PRIVATE_KEY\"]).decode())")
python3 /tmp/transip_delete_dns.py \
  --zone '"$TRANSIP_ZONE"' \
  --name '"$RECORD_NAME"' \
  --type TXT \
  --yes
' >&2 || echo "[cleanup-hook] WARN: delete failed or nothing to delete; continuing" >&2
