#!/usr/bin/env python3
"""Add (or replace) a single DNS record in a TransIP zone via REST API v6.

The TransIP account has an IP whitelist on its API key. Calls must originate from
a whitelisted source — in our setup that means: run this script from a pod inside
the production cluster. Local execution returns 401.

Reads credentials from environment:
  TRANSIP_ACCOUNT_NAME    TransIP login
  TRANSIP_PRIVATE_KEY     PEM-encoded RSA private key (the whole key, not a path)

Usage example (TXT record for Let's Encrypt DNS-01 challenge):
  python transip_set_dns.py --zone rijksapp.nl --name _acme-challenge.sandbox \
      --type TXT --content "eQY7FiBCoUerIiy2UX6hVz7KpLqeD001kcrMueezNQs" --expire 60

If a record with the same (name, type, content) already exists, the script is
idempotent and does nothing. If a record with the same (name, type) exists but
different content, use --replace to delete it first.

Exit codes: 0 = ok, 1 = already exists (no-op), 3 = API error.
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
    req = urllib.request.Request(  # noqa: S310 - API_BASE is hardcoded https://
        f"{API_BASE}/auth",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Signature": base64.b64encode(signature).decode(),
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - API_BASE is hardcoded https://
        return json.loads(resp.read())["token"]


def list_records(token: str, zone: str) -> list[dict]:
    req = urllib.request.Request(  # noqa: S310 - API_BASE is hardcoded https://
        f"{API_BASE}/domains/{zone}/dns",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - API_BASE is hardcoded https://
        return json.loads(resp.read())["dnsEntries"]


def add_record(token: str, zone: str, record: dict) -> None:
    req = urllib.request.Request(  # noqa: S310 - API_BASE is hardcoded https://
        f"{API_BASE}/domains/{zone}/dns",
        data=json.dumps({"dnsEntry": record}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req):  # noqa: S310 - API_BASE is hardcoded https://
        pass  # 201 Created on success


def delete_record(token: str, zone: str, record: dict) -> None:
    req = urllib.request.Request(  # noqa: S310 - API_BASE is hardcoded https://
        f"{API_BASE}/domains/{zone}/dns",
        data=json.dumps({"dnsEntry": record}).encode(),
        method="DELETE",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req):  # noqa: S310 - API_BASE is hardcoded https://
        pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zone", required=True, help="Zone, e.g. rijksapp.nl")
    p.add_argument("--name", required=True, help="Record name (label), e.g. '_acme-challenge.sandbox'")
    p.add_argument("--type", required=True, choices=["TXT", "A", "AAAA", "CNAME"], help="Record type")
    p.add_argument("--content", required=True, help="Record content (TXT value, IP, hostname)")
    p.add_argument("--expire", type=int, default=60, help="TTL in seconds (default 60)")
    p.add_argument(
        "--replace",
        action="store_true",
        help="If a record with same (name,type) exists with different content, delete it first.",
    )
    args = p.parse_args()

    account = os.environ.get("TRANSIP_ACCOUNT_NAME")
    key_pem = os.environ.get("TRANSIP_PRIVATE_KEY")
    if not account or not key_pem:
        print("ERROR: set TRANSIP_ACCOUNT_NAME and TRANSIP_PRIVATE_KEY env vars", file=sys.stderr)
        return 3

    new_record = {
        "name": args.name,
        "expire": args.expire,
        "type": args.type,
        "content": args.content,
    }

    try:
        token = get_token(account, key_pem.encode(), label=f"transip-set-{secrets.token_hex(4)}")
        existing = list_records(token, args.zone)
    except urllib.error.HTTPError as e:
        print(f"API error: {e.code} {e.reason} - {e.read().decode()}", file=sys.stderr)
        return 3

    same = [r for r in existing if r["name"] == args.name and r["type"] == args.type and r["content"] == args.content]
    if same:
        print(f"Record already exists in zone {args.zone}: {args.name} {args.type} -> {args.content}")
        return 1

    conflicting = [r for r in existing if r["name"] == args.name and r["type"] == args.type]
    if conflicting and args.replace:
        for r in conflicting:
            try:
                delete_record(token, args.zone, r)
                print(f"  deleted existing: {r['name']} {r['type']} -> {r['content']}")
            except urllib.error.HTTPError as e:
                print(f"  FAILED to delete: {r['name']} {r['type']}: {e.code} {e.read().decode()}", file=sys.stderr)
                return 3
    elif conflicting:
        print(f"WARNING: {len(conflicting)} record(s) with same name/type already exist (use --replace to overwrite):")
        for r in conflicting:
            print(f"  {r['name']:40s} {r['type']:6s} TTL={r['expire']:5d}  {r['content']}")

    try:
        add_record(token, args.zone, new_record)
        print(f"Added: {args.name} {args.type} TTL={args.expire} -> {args.content}")
    except urllib.error.HTTPError as e:
        print(f"API error on add: {e.code} {e.reason} - {e.read().decode()}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
