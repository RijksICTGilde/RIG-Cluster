#!/usr/bin/env python3
"""
Keycloak debugging script for investigating realm/client configurations.

Usage:
    python keycloak_debug.py --url https://keycloak.kind --user admin --password changeMe123!

    # List all realms
    python keycloak_debug.py ... list-realms

    # Get realm details including themes
    python keycloak_debug.py ... get-realm mijn-bureau-docs-local

    # List clients in a realm
    python keycloak_debug.py ... list-clients mijn-bureau-docs-local

    # Get client details
    python keycloak_debug.py ... get-client mijn-bureau-docs-local operations-manager-invites

    # Get all theme settings for a realm
    python keycloak_debug.py ... get-themes mijn-bureau-docs-local
"""

import argparse
import json
import sys
from typing import Any

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError


def create_admin(url: str, username: str, password: str, verify_ssl: bool = False) -> KeycloakAdmin:
    """Create a KeycloakAdmin instance."""
    return KeycloakAdmin(
        server_url=url,
        username=username,
        password=password,
        realm_name="master",
        user_realm_name="master",
        verify=verify_ssl,
    )


def print_json(data: Any) -> None:
    """Pretty print JSON data."""
    print(json.dumps(data, indent=2, default=str))


def list_realms(admin: KeycloakAdmin) -> None:
    """List all realms."""
    realms = admin.get_realms()
    print(f"Found {len(realms)} realms:\n")
    for realm in realms:
        print(f"  - {realm['realm']}")
        if realm.get("displayName"):
            print(f"    Display: {realm['displayName']}")
        print(f"    Login Theme: {realm.get('loginTheme', 'default')}")
        print()


def get_realm(admin: KeycloakAdmin, realm_name: str) -> None:
    """Get detailed realm information."""
    admin.connection.realm_name = realm_name
    realm = admin.get_realm(realm_name)

    print(f"Realm: {realm_name}")
    print("-" * 50)

    # Theme settings
    print("\nTheme Settings:")
    print(f"  Login Theme:   {realm.get('loginTheme', 'default')}")
    print(f"  Account Theme: {realm.get('accountTheme', 'default')}")
    print(f"  Admin Theme:   {realm.get('adminTheme', 'default')}")
    print(f"  Email Theme:   {realm.get('emailTheme', 'default')}")

    # Authentication settings
    print("\nAuthentication Flows:")
    print(f"  Browser Flow:  {realm.get('browserFlow', 'browser')}")
    print(f"  Direct Grant:  {realm.get('directGrantFlow', 'direct grant')}")

    # Other relevant settings
    print("\nOther Settings:")
    print(f"  Enabled:       {realm.get('enabled', False)}")
    print(f"  Registration:  {realm.get('registrationAllowed', False)}")
    print(f"  Remember Me:   {realm.get('rememberMe', False)}")


def get_themes(admin: KeycloakAdmin, realm_name: str) -> None:
    """Get theme settings for a realm and all its clients."""
    admin.connection.realm_name = realm_name

    # Get realm themes
    realm = admin.get_realm(realm_name)
    print(f"Realm '{realm_name}' Theme Settings:")
    print("-" * 50)
    print(f"  Login Theme:   {realm.get('loginTheme', '(default)')}")
    print(f"  Account Theme: {realm.get('accountTheme', '(default)')}")
    print(f"  Admin Theme:   {realm.get('adminTheme', '(default)')}")
    print(f"  Email Theme:   {realm.get('emailTheme', '(default)')}")

    # Get client-specific themes
    clients = admin.get_clients()
    print("\nClient Theme Overrides:")
    print("-" * 50)

    clients_with_themes = []
    for client in clients:
        # Check for theme-related attributes
        attributes = client.get("attributes", {})
        login_theme = attributes.get("login_theme")

        # Also check direct property (some Keycloak versions)
        if not login_theme:
            login_theme = client.get("loginTheme")

        if login_theme:
            clients_with_themes.append({"clientId": client["clientId"], "loginTheme": login_theme})

    if clients_with_themes:
        for c in clients_with_themes:
            print(f"  {c['clientId']}: {c['loginTheme']}")
    else:
        print("  (No client-specific theme overrides)")


def list_clients(admin: KeycloakAdmin, realm_name: str) -> None:
    """List all clients in a realm."""
    admin.connection.realm_name = realm_name
    clients = admin.get_clients()

    print(f"Found {len(clients)} clients in realm '{realm_name}':\n")
    for client in clients:
        client_id = client.get("clientId", "unknown")
        enabled = client.get("enabled", False)
        public = client.get("publicClient", False)

        print(f"  - {client_id}")
        print(f"    Enabled: {enabled}, Public: {public}")

        # Check for theme override
        attributes = client.get("attributes", {})
        if "login_theme" in attributes:
            print(f"    Login Theme Override: {attributes['login_theme']}")
        print()


def get_client(admin: KeycloakAdmin, realm_name: str, client_id: str) -> None:
    """Get detailed client information."""
    admin.connection.realm_name = realm_name

    # Find client by clientId
    clients = admin.get_clients()
    client = None
    for c in clients:
        if c.get("clientId") == client_id:
            client = c
            break

    if not client:
        print(f"Client '{client_id}' not found in realm '{realm_name}'")
        return

    print(f"Client: {client_id}")
    print("-" * 50)

    # Basic info
    print("\nBasic Settings:")
    print(f"  ID:            {client.get('id')}")
    print(f"  Enabled:       {client.get('enabled', False)}")
    print(f"  Public Client: {client.get('publicClient', False)}")
    print(f"  Protocol:      {client.get('protocol', 'openid-connect')}")

    # URLs
    print("\nURLs:")
    print(f"  Root URL:      {client.get('rootUrl', '(not set)')}")
    print(f"  Base URL:      {client.get('baseUrl', '(not set)')}")
    redirect_uris = client.get("redirectUris", [])
    print(f"  Redirect URIs: {redirect_uris}")

    # Theme settings
    print("\nTheme Settings:")
    attributes = client.get("attributes", {})
    login_theme = attributes.get("login_theme") or client.get("loginTheme")
    print(f"  Login Theme:   {login_theme or '(inherits from realm)'}")

    # All attributes
    if attributes:
        print("\nAll Attributes:")
        for key, value in attributes.items():
            print(f"  {key}: {value}")

    # Full dump option
    print("\nFull Client Config:")
    print_json(client)


def set_client_theme(admin: KeycloakAdmin, realm_name: str, client_id: str, theme: str | None) -> None:
    """Set or clear the login theme for a client."""
    admin.connection.realm_name = realm_name

    # Find client by clientId
    clients = admin.get_clients()
    client = None
    for c in clients:
        if c.get("clientId") == client_id:
            client = c
            break

    if not client:
        print(f"Client '{client_id}' not found in realm '{realm_name}'")
        return

    client_uuid = client["id"]
    attributes = client.get("attributes", {})

    if theme:
        # Set theme
        attributes["login_theme"] = theme
        print(f"Setting login theme for '{client_id}' to '{theme}'")
    else:
        # Clear theme (inherit from realm)
        if "login_theme" in attributes:
            del attributes["login_theme"]
        print(f"Clearing login theme override for '{client_id}' (will inherit from realm)")

    # Update client
    admin.update_client(client_uuid, {"attributes": attributes})
    print("Done!")


def main():
    parser = argparse.ArgumentParser(description="Keycloak debugging tool")
    parser.add_argument("--url", default="https://keycloak.kind", help="Keycloak URL")
    parser.add_argument("--user", "-u", default="admin", help="Admin username")
    parser.add_argument("--password", "-p", required=True, help="Admin password")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify SSL certificates")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # list-realms
    subparsers.add_parser("list-realms", help="List all realms")

    # get-realm
    p = subparsers.add_parser("get-realm", help="Get realm details")
    p.add_argument("realm", help="Realm name")

    # get-themes
    p = subparsers.add_parser("get-themes", help="Get theme settings for realm and clients")
    p.add_argument("realm", help="Realm name")

    # list-clients
    p = subparsers.add_parser("list-clients", help="List clients in a realm")
    p.add_argument("realm", help="Realm name")

    # get-client
    p = subparsers.add_parser("get-client", help="Get client details")
    p.add_argument("realm", help="Realm name")
    p.add_argument("client_id", help="Client ID")

    # set-client-theme
    p = subparsers.add_parser("set-client-theme", help="Set login theme for a client")
    p.add_argument("realm", help="Realm name")
    p.add_argument("client_id", help="Client ID")
    p.add_argument("--theme", help="Theme name (omit to clear and inherit from realm)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        admin = create_admin(args.url, args.user, args.password, args.verify_ssl)

        if args.command == "list-realms":
            list_realms(admin)
        elif args.command == "get-realm":
            get_realm(admin, args.realm)
        elif args.command == "get-themes":
            get_themes(admin, args.realm)
        elif args.command == "list-clients":
            list_clients(admin, args.realm)
        elif args.command == "get-client":
            get_client(admin, args.realm, args.client_id)
        elif args.command == "set-client-theme":
            set_client_theme(admin, args.realm, args.client_id, args.theme)

    except KeycloakError as e:
        print(f"Keycloak error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
