#!/usr/bin/env bash
# Renew the *.sandbox.rijksapp.dev wildcard Let's Encrypt certificate via DNS-01.
#
# Uses the TransIP API to set/clean the _acme-challenge TXT record. Because the
# TransIP API key is IP-whitelisted, the actual TransIP calls go via a kubectl
# exec into the operations-manager pod in the production cluster.
#
# Prereqs:
#   - certbot (brew install certbot)
#   - kubectl context pointing at the production cluster
#   - You have read access to secret/transip-credentials in $OPI_NAMESPACE
#
# Usage:
#   scripts/renew-sandbox-cert.sh
#   DOMAIN='*.sandbox.example.com' TRANSIP_ZONE=example.com scripts/renew-sandbox-cert.sh
set -euo pipefail

DOMAIN="${DOMAIN:-*.sandbox.rijksapp.dev}"
TRANSIP_ZONE="${TRANSIP_ZONE:-rijksapp.dev}"
EMAIL="${EMAIL:-robbert.uittenbroek@rijksoverheid.nl}"
OPI_NAMESPACE="${OPI_NAMESPACE:-rig-prd-operations}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Find OPI pod
OPI_POD=$(kubectl -n "$OPI_NAMESPACE" get pods -l app=operations-manager -o jsonpath='{.items[0].metadata.name}')
if [[ -z "$OPI_POD" ]]; then
  echo "ERROR: no operations-manager pod found in $OPI_NAMESPACE" >&2
  exit 1
fi
echo "Using OPI pod: $OPI_POD (namespace=$OPI_NAMESPACE)"
echo "Domain: $DOMAIN"
echo "Zone:   $TRANSIP_ZONE"
echo "Email:  $EMAIL"

# Copy helper scripts into pod /tmp (idempotent)
kubectl -n "$OPI_NAMESPACE" cp "$REPO_ROOT/operations-manager/python/scripts/transip_set_dns.py" "$OPI_POD:/tmp/transip_set_dns.py"
kubectl -n "$OPI_NAMESPACE" cp "$REPO_ROOT/operations-manager/python/scripts/transip_delete_dns.py" "$OPI_POD:/tmp/transip_delete_dns.py"

# Export for hooks
export OPI_POD OPI_NAMESPACE TRANSIP_ZONE

# Use a temp directory so we don't need sudo and don't pollute /etc/letsencrypt
WORKDIR="$(mktemp -d -t certbot-sandbox.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

certbot certonly \
  --manual \
  --preferred-challenges dns \
  --manual-auth-hook "$REPO_ROOT/scripts/certbot/auth-hook.sh" \
  --manual-cleanup-hook "$REPO_ROOT/scripts/certbot/cleanup-hook.sh" \
  --config-dir "$WORKDIR/config" \
  --work-dir "$WORKDIR/work" \
  --logs-dir "$WORKDIR/logs" \
  --agree-tos \
  --no-eff-email \
  --email "$EMAIL" \
  --non-interactive \
  -d "$DOMAIN"

# Locate the issued cert
CERT_DIR=$(find "$WORKDIR/config/live" -mindepth 1 -maxdepth 1 -type d ! -name "README" | head -1)
if [[ -z "$CERT_DIR" || ! -f "$CERT_DIR/fullchain.pem" ]]; then
  echo "ERROR: no fullchain.pem found in $WORKDIR/config/live" >&2
  exit 1
fi

OUT_DIR="$REPO_ROOT/security/tls/sandbox-wildcard"
mkdir -p "$OUT_DIR"
cp -L "$CERT_DIR/fullchain.pem" "$OUT_DIR/fullchain.pem"
cp -L "$CERT_DIR/privkey.pem" "$OUT_DIR/privkey.pem"

echo
echo "Certificate written to:"
echo "  $OUT_DIR/fullchain.pem"
echo "  $OUT_DIR/privkey.pem"
echo
openssl x509 -in "$OUT_DIR/fullchain.pem" -noout -dates -subject -ext subjectAltName

# Re-encrypt to .age using the recipient derived from security/developer-key.txt
DEV_KEY="$REPO_ROOT/security/developer-key.txt"
if [[ -f "$DEV_KEY" ]]; then
  RECIPIENT=$(age-keygen -y "$DEV_KEY")
  echo
  echo "Re-encrypting .age files with recipient $RECIPIENT"
  age -e -r "$RECIPIENT" -o "$OUT_DIR/fullchain.pem.age" "$OUT_DIR/fullchain.pem"
  age -e -r "$RECIPIENT" -o "$OUT_DIR/privkey.pem.age" "$OUT_DIR/privkey.pem"
  echo "  $OUT_DIR/fullchain.pem.age"
  echo "  $OUT_DIR/privkey.pem.age"
else
  echo
  echo "WARN: $DEV_KEY not found — skipping .age re-encryption. Encrypt manually before commit."
fi

echo
echo "Next steps:"
echo "  1. Commit only the .age files (.gitignore should keep the plaintext PEMs out)."
echo "  2. Import to running sandbox:  task sandbox:import-wildcard-cert"
