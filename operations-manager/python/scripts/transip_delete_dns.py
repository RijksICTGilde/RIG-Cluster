#!/usr/bin/env python3
"""Delete one or more DNS records from a TransIP zone via REST API v6.

The TransIP account has an IP whitelist on its API key. Calls must originate from
a whitelisted source — in our setup that means: run this script from a pod inside
the production cluster (e.g. operations-manager). Local execution returns 401.

Reads credentials from environment:
  TRANSIP_ACCOUNT_NAME    TransIP login (e.g. 'digigilde')
  TRANSIP_PRIVATE_KEY     PEM-encoded RSA private key (the whole key, not a path)

Usage examples:
  # List matching records (no delete):
  python transip_delete_dns.py --zone rijksapp.nl --name amt --dry-run

  # Delete amt CNAME from rijksapp.nl:
  python transip_delete_dns.py --zone rijksapp.nl --name amt --type CNAME

  # Delete every CNAME in the zone whose target points at the broken router:
  python transip_delete_dns.py --zone rijksapp.nl --target-equals \
      router-rig.rig.prd1.gn2.quattro.rijksapps.nl. --type CNAME

Exit codes: 0 = ok, 1 = nothing matched, 2 = aborted by user, 3 = API error.

See docs/dns-router-zone-migration.md for the full migration runbook.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

API_BASE = "https://api.transip.nl/v6"


def get_token(account: str, key_pem: bytes, label: str) -> str:
    body = json.dumps(
        {
            "login": account,
            "nonce": secrets.token_hex(16),
            "read_only": False,
            "expiration_time": "30 minutes",
            "label": label,
            "global_key": True,
        },
        separators=(",", ":"),
    ).encode()
    private_key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(private_key, RSAPrivateKey):
        raise SystemExit("Expected an RSA private key for TransIP API auth.")
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA512())
    req = urllib.request.Request(
        f"{API_BASE}/auth",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Signature": base64.b64encode(signature).decode(),
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["token"]


def list_records(token: str, zone: str) -> list[dict]:
    req = urllib.request.Request(
        f"{API_BASE}/domains/{zone}/dns",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["dnsEntries"]


def delete_record(token: str, zone: str, record: dict) -> None:
    req = urllib.request.Request(
        f"{API_BASE}/domains/{zone}/dns",
        data=json.dumps({"dnsEntry": record}).encode(),
        method="DELETE",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req):
        pass  # 204 No Content on success


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zone", required=True, help="Zone, e.g. rijksapp.nl")
    p.add_argument("--name", help="Record name (label, e.g. 'amt'). Omit to match all names.")
    p.add_argument("--type", help="Record type filter (CNAME, A, AAAA, TXT). Omit to match all.")
    p.add_argument("--target-equals", help="Only match records whose content equals this exact string.")
    p.add_argument("--dry-run", action="store_true", help="List matches but do not delete.")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    args = p.parse_args()

    account = os.environ.get("TRANSIP_ACCOUNT_NAME")
    key_pem = os.environ.get("TRANSIP_PRIVATE_KEY")
    if not account or not key_pem:
        print("ERROR: set TRANSIP_ACCOUNT_NAME and TRANSIP_PRIVATE_KEY env vars", file=sys.stderr)
        return 3

    try:
        token = get_token(account, key_pem.encode(), label=f"transip-delete-{secrets.token_hex(4)}")
        records = list_records(token, args.zone)
    except urllib.error.HTTPError as e:
        print(f"API error: {e.code} {e.reason} - {e.read().decode()}", file=sys.stderr)
        return 3

    matches = [
        r
        for r in records
        if (args.name is None or r["name"] == args.name)
        and (args.type is None or r["type"] == args.type)
        and (args.target_equals is None or r["content"] == args.target_equals)
    ]

    if not matches:
        print(f"No matching records in zone {args.zone}", file=sys.stderr)
        return 1

    print(f"Matched {len(matches)} record(s) in {args.zone}:")
    for r in matches:
        print(f"  {r['name']:40s} {r['type']:6s} TTL={r['expire']:5d}  {r['content']}")

    if args.dry_run:
        print("\n(--dry-run set, not deleting)")
        return 0

    if not args.yes:
        confirm = input(f"\nDelete {len(matches)} record(s)? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return 2

    for r in matches:
        try:
            delete_record(token, args.zone, r)
            print(f"  deleted: {r['name']} {r['type']} -> {r['content']}")
        except urllib.error.HTTPError as e:
            print(f"  FAILED: {r['name']} {r['type']}: {e.code} {e.read().decode()}", file=sys.stderr)
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
