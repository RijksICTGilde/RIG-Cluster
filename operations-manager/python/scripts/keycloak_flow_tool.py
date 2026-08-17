#!/usr/bin/env python3
"""Keycloak auth-flow tool: inspect and (re)build the ZAD auto-link first-broker-login flow.

Talks directly to a Keycloak admin API (default https://keycloak.rijksapp.nl) with the same
python-keycloak library ZAD uses, so flow actions are fast and repeatable (no `kubectl exec`).

Why this exists: building a Keycloak auth flow via the admin API does NOT give a deterministic
execution order (keycloak#43016). The `index` in a PUT is ignored (keycloak#8726) and
raise-priority is a no-op when siblings share the same priority (the tie all-zero case). The
reliable fix, confirmed by keycloak-config-cli, is to set an explicit `priority` in the
execution CREATE body, which Keycloak >= 25 honors. `rebuild` deletes the flow and recreates it
with explicit, gapped priorities so `idp-create-user-if-unique` always precedes the
handle-existing subflow (otherwise idp-auto-link runs with no existing-user context and linking
a pre-created account fails with "Invalid username or password").

Usage (run from operations-manager/python, so `opi`/python-keycloak are importable):
    export KEYCLOAK_ADMIN_PASSWORD=...            # required
    # optional: KEYCLOAK_URL (default https://keycloak.rijksapp.nl), KEYCLOAK_ADMIN_USER (admin)

    uv run python scripts/keycloak_flow_tool.py inspect <realm>
    uv run python scripts/keycloak_flow_tool.py rebuild <realm> [--confirm-link]
    uv run python scripts/keycloak_flow_tool.py inspect-all          # scan every realm, report order
    uv run python scripts/keycloak_flow_tool.py ensure-redirector <realm>   # unblock sso-support -> sso-only

Exit code is non-zero if a realm's flow is in the wrong (broken) order.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakGetError, KeycloakPostError

FLOW = "first broker login auto-link"
UCO = f"{FLOW} user creation or linking"
HEA = f"{FLOW} handle existing account"
IDP_ALIAS = "rig-platform-oidc"
STOCK_FLOW = "first broker login"

# Explicit leaf priorities so there are no ties and order is deterministic (Keycloak >= 25).
# Subflows are not given an explicit priority; they take getNextPriority = max(existing)+1, so
# creating idp-create-user-if-unique (priority 10) before the handle-existing subflow lands the
# subflow at 11 (after it).
PRIO_REVIEW = 10
PRIO_CREATE_USER = 10
PRIO_CONFIRM_LINK = 10
PRIO_AUTO_LINK = 20

REDIRECTOR_FLOW = "External IDP Redirector"


def connect() -> KeycloakAdmin:
    pw = os.environ.get("KEYCLOAK_ADMIN_PASSWORD")
    if not pw:
        sys.exit("KEYCLOAK_ADMIN_PASSWORD not set")
    admin = KeycloakAdmin(
        server_url=os.environ.get("KEYCLOAK_URL", "https://keycloak.rijksapp.nl"),
        username=os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
        password=pw,
        realm_name="master",
        user_realm_name="master",
        verify=True,
    )
    return admin


def _find_flow_id(admin: KeycloakAdmin, alias: str) -> str | None:
    for f in admin.get_authentication_flows():
        if f.get("alias") == alias:
            return f.get("id")
    return None


def inspect(admin: KeycloakAdmin, realm: str) -> bool:
    """Print the flow order; return True if correct (create-user before handle-existing)."""
    admin.change_current_realm(realm)
    execs = admin.get_authentication_flow_executions(flow_alias=UCO) if _find_flow_id(admin, FLOW) else None
    print(f"\n=== {realm} ===")
    if execs is None:
        print("  (no auto-link flow)")
        return True
    order = [e for e in execs if e.get("level") == 0]
    for e in execs:
        label = e.get("providerId") or f"[subflow] {e.get('displayName')}"
        print(f"  L{e.get('level')} idx{e.get('index')} prio={e.get('priority')} req={e.get('requirement')} {label}")
    create_idx = next((e.get("index") for e in order if e.get("providerId") == "idp-create-user-if-unique"), None)
    hea_idx = next((e.get("index") for e in order if e.get("authenticationFlow") and e.get("displayName") == HEA), None)
    ok = create_idx is not None and hea_idx is not None and create_idx < hea_idx
    print(f"  -> order {'OK' if ok else 'BROKEN (create-user must precede handle-existing)'}")
    return ok


def _create_execution(admin: KeycloakAdmin, flow_alias: str, provider: str, requirement: str, priority: int) -> None:
    # Keycloak >= 25 honors "priority" in the create body; requirement still needs a follow-up update.
    admin.create_authentication_flow_execution(
        payload={"provider": provider, "priority": priority}, flow_alias=flow_alias
    )
    execs = admin.get_authentication_flow_executions(flow_alias=flow_alias)
    target = next((e for e in reversed(execs) if e.get("providerId") == provider), None)
    if target and target.get("requirement") != requirement:
        target["requirement"] = requirement
        admin.update_authentication_flow_executions(payload=target, flow_alias=flow_alias)


def _create_subflow(admin: KeycloakAdmin, parent_alias: str, subflow_alias: str, requirement: str) -> None:
    admin.create_authentication_flow_subflow(
        payload={
            "alias": subflow_alias,
            "type": "basic-flow",
            "provider": "registration-page-form",
            "description": subflow_alias,
        },
        flow_alias=parent_alias,
        skip_exists=True,
    )
    execs = admin.get_authentication_flow_executions(flow_alias=parent_alias)
    binding = next((e for e in execs if e.get("displayName") == subflow_alias and e.get("authenticationFlow")), None)
    if binding and binding.get("requirement") != requirement:
        binding["requirement"] = requirement
        admin.update_authentication_flow_executions(payload=binding, flow_alias=parent_alias)


def rebuild(admin: KeycloakAdmin, realm: str, confirm_link: bool) -> bool:
    """Delete and recreate the auto-link flow with explicit priorities. Returns True if verified."""
    admin.change_current_realm(realm)

    # Repoint the IdP to the stock flow so the auto-link flow can be deleted, then delete it.
    idp = None
    with contextlib.suppress(KeycloakGetError):
        idp = admin.get_idp(IDP_ALIAS)
    if idp is not None and idp.get("firstBrokerLoginFlowAlias") == FLOW:
        idp["firstBrokerLoginFlowAlias"] = STOCK_FLOW
        admin.update_idp(IDP_ALIAS, idp)
        print(f"  repointed IdP {IDP_ALIAS} -> '{STOCK_FLOW}' (temporary)")

    flow_id = _find_flow_id(admin, FLOW)
    if flow_id:
        admin.delete_authentication_flow(flow_id)
        print(f"  deleted old '{FLOW}'")

    # Recreate with explicit, gapped priorities (create-user < handle-existing).
    admin.create_authentication_flow(
        payload={
            "alias": FLOW,
            "description": "Auto-link a brokered SSO identity to a pre-existing account",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        }
    )
    _create_execution(admin, FLOW, "idp-review-profile", "DISABLED", PRIO_REVIEW)
    _create_subflow(admin, FLOW, UCO, "REQUIRED")
    _create_execution(admin, UCO, "idp-create-user-if-unique", "ALTERNATIVE", PRIO_CREATE_USER)
    _create_subflow(admin, UCO, HEA, "ALTERNATIVE")
    if confirm_link:
        _create_execution(admin, HEA, "idp-confirm-link", "REQUIRED", PRIO_CONFIRM_LINK)
    _create_execution(admin, HEA, "idp-auto-link", "REQUIRED", PRIO_AUTO_LINK)

    # Repoint the IdP back to the auto-link flow.
    if idp is not None:
        idp = admin.get_idp(IDP_ALIAS)
        idp["firstBrokerLoginFlowAlias"] = FLOW
        admin.update_idp(IDP_ALIAS, idp)
        print(f"  repointed IdP {IDP_ALIAS} -> '{FLOW}'")

    ok = inspect(admin, realm)
    return ok


def ensure_redirector_shell(admin: KeycloakAdmin, realm: str) -> bool:
    """Create the empty '{REDIRECTOR_FLOW}' top-level flow so OPI can switch a realm to sso-only.

    OPI sets the realm's browserFlow to this alias BEFORE the call that creates the flow, and
    Keycloak answers an unknown alias with a bare 500 {"errorMessage":"Failed to update realm"}.
    So a realm created on sso-support cannot be moved to sso-only until the alias exists.

    Only the empty shell is created here. OPI's ``_create_external_idp_redirector_flow`` is
    409-tolerant on the flow and adds each execution only when missing, so the next project
    processing fills in auth-cookie plus identity-provider-redirector and points browserFlow at
    it. Drop this subcommand once the ordering fix in ``keycloak_manager`` is deployed.
    """
    admin.change_current_realm(realm)
    if _find_flow_id(admin, REDIRECTOR_FLOW):
        print(f"  '{REDIRECTOR_FLOW}' already exists in {realm}, nothing to do")
        return True
    admin.create_authentication_flow(
        payload={
            "alias": REDIRECTOR_FLOW,
            "description": "External IDP Redirector flow for automatic SSO redirect",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        }
    )
    created = _find_flow_id(admin, REDIRECTOR_FLOW) is not None
    print(f"  created empty '{REDIRECTOR_FLOW}' in {realm}" if created else "  creation reported no flow")
    print("  next: run 'Project herverwerken' so OPI fills in the executions")
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ins = sub.add_parser("inspect", help="print a realm's auto-link flow order")
    p_ins.add_argument("realm")
    p_reb = sub.add_parser("rebuild", help="delete+recreate a realm's auto-link flow with correct order")
    p_reb.add_argument("realm")
    p_reb.add_argument("--confirm-link", action="store_true", help="include the idp-confirm-link screen")
    sub.add_parser("inspect-all", help="scan every realm and report flow order")
    p_red = sub.add_parser(
        "ensure-redirector",
        help="create the empty 'External IDP Redirector' flow so a realm can move to sso-only",
    )
    p_red.add_argument("realm")
    args = parser.parse_args()

    admin = connect()

    if args.cmd == "inspect":
        sys.exit(0 if inspect(admin, args.realm) else 1)
    elif args.cmd == "rebuild":
        try:
            ok = rebuild(admin, args.realm, args.confirm_link)
        except KeycloakPostError as e:
            sys.exit(f"rebuild failed: {e}")
        sys.exit(0 if ok else 1)
    elif args.cmd == "ensure-redirector":
        print(f"\n=== {args.realm} ===")
        try:
            ok = ensure_redirector_shell(admin, args.realm)
        except KeycloakPostError as e:
            sys.exit(f"ensure-redirector failed: {e}")
        sys.exit(0 if ok else 1)
    elif args.cmd == "inspect-all":
        broken = []
        for f in admin.get_realms():
            realm = f.get("realm")
            admin.change_current_realm(realm)
            if _find_flow_id(admin, FLOW) and not inspect(admin, realm):
                broken.append(realm)
        print(f"\nbroken realms: {broken or 'none'}")
        sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
