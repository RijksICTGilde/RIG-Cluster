#!/usr/bin/env python3
"""Pre-flight report for closing off identity self-service on Keycloak realms.

Run this BEFORE the disabled `UPDATE_PASSWORD` required action reaches a realm. Disabling a
required action does not touch what is already there, so two populations need checking first
(see features/futures/keycloak-sso-bypass-voorkomen.md):

  1. Users with a PENDING `UPDATE_PASSWORD` required action. Once the action is disabled it can
     no longer run, so these users can get stuck at login. Typically caused by an admin setting
     a password with "Temporary" on. This is the blocking finding: the report exits non-zero.

  2. Federated users who already have a password credential. Closing the knob changes nothing
     for them, they keep the bypass (log in locally, skipping SSO Rijk). Cleanup item, not a
     blocker, so it does not affect the exit code.

Report only. Nothing is deleted or changed. The cleanup command is printed per finding.

Usage (run from operations-manager/python, so python-keycloak is importable):
    export KEYCLOAK_ADMIN_PASSWORD=...            # required
    # optional: KEYCLOAK_URL (default https://keycloak.rijksapp.nl), KEYCLOAK_ADMIN_USER (admin)

    uv run python scripts/keycloak_self_service_report.py                 # every realm but master
    uv run python scripts/keycloak_self_service_report.py --realm rig-platform
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError

REQUIRED_ACTION_ALIAS = "UPDATE_PASSWORD"


def connect() -> KeycloakAdmin:
    pw = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not pw:
        sys.exit("KEYCLOAK_ADMIN_PASSWORD not set")
    return KeycloakAdmin(
        server_url=os.environ.get("KEYCLOAK_URL", "https://keycloak.rijksapp.nl"),
        username=os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
        password=pw,
        realm_name="master",
        user_realm_name="master",
        verify=True,
    )


def federated_identities(admin: KeycloakAdmin, realm: str, user_id: str) -> list[dict[str, Any]]:
    """The user's IdP links. python-keycloak has no wrapper for this endpoint."""
    response = admin.connection.raw_get(f"admin/realms/{realm}/users/{user_id}/federated-identity")
    response.raise_for_status()
    return response.json()


def scan_realm(admin: KeycloakAdmin, realm: str) -> tuple[list[dict], list[dict]]:
    """Return (users that would get stuck, federated users holding a password)."""
    admin.change_current_realm(realm)
    stuck: list[dict] = []
    bypass: list[dict] = []

    for user in admin.get_users({}):
        user_id = user["id"]
        username = user.get("username", user_id)

        if REQUIRED_ACTION_ALIAS in (user.get("requiredActions") or []):
            stuck.append({"realm": realm, "id": user_id, "username": username})

        credentials = admin.get_credentials(user_id)
        password = next((c for c in credentials if c.get("type") == "password"), None)
        if password is None:
            continue

        links = federated_identities(admin, realm, user_id)
        if links:
            bypass.append(
                {
                    "realm": realm,
                    "id": user_id,
                    "username": username,
                    "credential_id": password.get("id"),
                    "providers": [link.get("identityProvider") for link in links],
                }
            )

    return stuck, bypass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--realm",
        action="append",
        dest="realms",
        help="Realm to scan (repeatable). Default: every realm except master.",
    )
    args = parser.parse_args()

    admin = connect()
    realms = args.realms or [r["realm"] for r in admin.get_realms() if r["realm"] != "master"]

    all_stuck: list[dict] = []
    all_bypass: list[dict] = []
    for realm in realms:
        try:
            stuck, bypass = scan_realm(admin, realm)
        except KeycloakError as e:
            print(f"! {realm}: could not scan ({e})")
            continue
        all_stuck.extend(stuck)
        all_bypass.extend(bypass)
        print(f"  {realm}: {len(stuck)} would get stuck, {len(bypass)} holding the bypass")

    print()
    print(f"Scanned {len(realms)} realm(s).")

    print()
    print(f"[blocking] Pending {REQUIRED_ACTION_ALIAS} required action: {len(all_stuck)}")
    for entry in all_stuck:
        print(f"  {entry['realm']}/{entry['username']} ({entry['id']})")
    if all_stuck:
        print("  Resolve before disabling the action, otherwise these users cannot complete login.")
        print("  Either let them finish the action first, or clear it on the user representation.")

    print()
    print(f"[cleanup] Federated users with a password credential: {len(all_bypass)}")
    for entry in all_bypass:
        providers = ", ".join(p for p in entry["providers"] if p)
        print(f"  {entry['realm']}/{entry['username']} ({entry['id']}) linked to {providers}")
        print(f"    DELETE /admin/realms/{entry['realm']}/users/{entry['id']}/credentials/{entry['credential_id']}")
    if all_bypass:
        print("  These keep the bypass until the password credential is removed. Report first, purge after.")

    return 1 if all_stuck else 0


if __name__ == "__main__":
    sys.exit(main())
