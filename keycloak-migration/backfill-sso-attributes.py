#!/usr/bin/env python3
"""
Backfill SSO-Rijk NameID attributes for existing users.

This script reads users' federatedIdentities (specifically the userId field which contains
the SAML NameID) and populates two user attributes:
  - sso-rijk-userid: Original NameID value
  - sso-rijk-userid-lowercase: Lowercase version for case-insensitive matching

These attributes should have been set by IDP mappers going forward, but this script
backfills them for users who logged in before the mappers were configured.

Usage:
    python backfill-sso-attributes.py <keycloak_url> <realm> <admin_user> [options]

Options:
    --dry-run                    Only show what would be changed, don't update
    --test-user <username>       Only process this specific user (good for testing)
    --debug                      Show detailed request/response information

Examples:
    # Dry-run to see what would change
    python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --dry-run

    # Test with single user
    python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --test-user robbert.uittenbroek

    # Dry-run for single user (recommended first step!)
    python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --test-user robbert.uittenbroek --dry-run

    # Debug single user to see request details
    python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --test-user robbert.uittenbroek --debug

    # Full run (after testing)
    python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin
"""

import getpass
import json
import sys
from copy import deepcopy

import requests


def get_admin_token(keycloak_url: str, admin_user: str, admin_password: str) -> str:
    """Get admin access token."""
    url = f"{keycloak_url}/realms/master/protocol/openid-connect/token"
    data = {
        "client_id": "admin-cli",
        "username": admin_user,
        "password": admin_password,
        "grant_type": "password",
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    return response.json()["access_token"]


def get_users(keycloak_url: str, realm: str, token: str) -> list[dict]:
    """Get all users in realm."""
    url = f"{keycloak_url}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}"}

    users = []
    first = 0
    max_results = 100

    while True:
        params = {"first": first, "max": max_results}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        batch = response.json()
        if not batch:
            break

        users.extend(batch)
        first += max_results

    return users


def get_user_by_username(
    keycloak_url: str, realm: str, token: str, username: str
) -> dict | None:
    """Get a specific user by username."""
    url = f"{keycloak_url}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"username": username, "exact": "true"}

    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()

    users = response.json()
    if users:
        return users[0]
    return None


def get_federated_identities(
    keycloak_url: str, realm: str, token: str, user_id: str
) -> list[dict]:
    """Get user's federated identities."""
    url = f"{keycloak_url}/admin/realms/{realm}/users/{user_id}/federated-identity"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def update_user_attributes(
    keycloak_url: str, realm: str, token: str, user_id: str, user: dict, attributes: dict, debug: bool = False
) -> None:
    """Update user attributes.

    Note: Keycloak 24.0.0+ has a bug where if you don't send firstName, lastName, email,
    those fields will be CLEARED even though they're marked as optional!

    See:
    - https://github.com/keycloak/keycloak/issues/27777 (no partial updates)
    - https://github.com/keycloak/keycloak/issues/28220 (clears firstName/lastName/email)

    Workaround: Send minimal required fields to preserve data.
    """
    url = f"{keycloak_url}/admin/realms/{realm}/users/{user_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Build minimal user data to avoid clearing fields
    # Include firstName, lastName, email to prevent them from being cleared
    user_data = {
        "username": user.get("username"),
        "enabled": user.get("enabled", True),
        "emailVerified": user.get("emailVerified", False),
        "attributes": attributes,
    }

    # CRITICAL: Include firstName, lastName, email if they exist (to prevent clearing)
    if user.get("firstName"):
        user_data["firstName"] = user.get("firstName")
    if user.get("lastName"):
        user_data["lastName"] = user.get("lastName")
    if user.get("email"):
        user_data["email"] = user.get("email")

    if debug:
        print("\n" + "=" * 80)
        print("DEBUG: Request Details")
        print("=" * 80)
        print(f"URL: {url}")
        print(f"Method: PUT")
        print(f"\nOriginal user attributes:")
        print(json.dumps(user.get("attributes", {}), indent=2))
        print(f"\nNew attributes being set:")
        print(json.dumps(attributes, indent=2))
        print(f"\nMinimal user data being sent (to preserve firstName/lastName/email):")
        print(json.dumps(user_data, indent=2))
        print("=" * 80 + "\n")

    response = requests.put(url, headers=headers, json=user_data)

    if debug:
        print(f"Response Status: {response.status_code}")
        if response.text:
            print(f"Response Body: {response.text}")
        print("=" * 80 + "\n")

    response.raise_for_status()


def get_user_by_id(keycloak_url: str, realm: str, token: str, user_id: str) -> dict:
    """Get full user representation by ID.

    CRITICAL: This returns the COMPLETE user object with ALL attributes.
    The list users endpoint might not return all attributes!
    """
    url = f"{keycloak_url}/admin/realms/{realm}/users/{user_id}"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def backfill_user(
    keycloak_url: str, realm: str, token: str, user: dict, dry_run: bool = False, debug: bool = False
) -> tuple[bool, str, dict | None]:
    """
    Backfill SSO-Rijk attributes for a single user.

    Returns:
        Tuple of (success: bool, message: str, details: dict | None)
        details contains: {'sso_userid': str, 'would_set': bool} for analysis
    """
    user_id = user["id"]
    username = user.get("username", "unknown")

    # CRITICAL: Get the FULL user object to ensure we have ALL current attributes
    # The user object from get_users() might not include all attributes!
    try:
        full_user = get_user_by_id(keycloak_url, realm, token, user_id)
        if debug:
            print(f"\n[DEBUG] Fetched full user object for {username}")
            print(f"[DEBUG] Current attributes: {json.dumps(full_user.get('attributes', {}), indent=2)}")
    except Exception as e:
        return False, f"Failed to get full user object: {e}", None

    # Get federated identities
    try:
        fed_identities = get_federated_identities(keycloak_url, realm, token, user_id)
    except Exception as e:
        return False, f"Failed to get federated identities: {e}", None

    # Find SSO-Rijk identity
    sso_rijk_identity = None
    for fed_id in fed_identities:
        if fed_id.get("identityProvider") == "sso-rijk":
            sso_rijk_identity = fed_id
            break

    if not sso_rijk_identity:
        return False, "No sso-rijk identity found", None

    # Extract the collab person ID from userId
    sso_userid = sso_rijk_identity.get("userId", "")
    sso_username = sso_rijk_identity.get("userName", "")

    if not sso_userid:
        return False, "sso-rijk identity has no userId", None

    # Get current attributes from the FULL user object (not the list response)
    current_attrs = full_user.get("attributes", {})

    # Check if already has the attributes
    has_original = "sso-rijk-userid" in current_attrs
    has_lowercase = "sso-rijk-userid-lowercase" in current_attrs

    if has_original and has_lowercase:
        existing_original = current_attrs["sso-rijk-userid"][0]
        existing_lowercase = current_attrs["sso-rijk-userid-lowercase"][0]
        details = {
            "sso_userid": sso_userid,
            "sso_username": sso_username,
            "existing_original": existing_original,
            "existing_lowercase": existing_lowercase,
            "would_set": False,
        }
        return (
            False,
            f"Already has sso-rijk-userid = {existing_original}",
            details,
        )

    # Set both attributes (Keycloak expects list values)
    # CRITICAL: We're MERGING with existing attributes, not replacing!
    # Original value from NameID
    current_attrs["sso-rijk-userid"] = [sso_userid]
    # Lowercase version for case-insensitive matching
    current_attrs["sso-rijk-userid-lowercase"] = [sso_userid.lower()]

    if debug:
        print(f"\n[DEBUG] After merge, current_attrs has {len(current_attrs)} keys:")
        for key in current_attrs.keys():
            print(f"  - {key}")

    details = {
        "sso_userid": sso_userid,
        "sso_username": sso_username,
        "sso_userid_lowercase": sso_userid.lower(),
        "would_set": True,
    }

    # Update user (unless dry-run)
    if not dry_run:
        try:
            update_user_attributes(keycloak_url, realm, token, user_id, full_user, current_attrs, debug)
        except Exception as e:
            return False, f"Failed to update attributes: {e}", details

    action = "Would set" if dry_run else "Set"
    return True, f"{action} sso-rijk-userid = {sso_userid}, sso-rijk-userid-lowercase = {sso_userid.lower()}", details


def main():
    # Parse arguments
    args = sys.argv[1:]

    if len(args) < 3:
        print(
            "Usage: python backfill-sso-attributes.py <keycloak_url> <realm> <admin_user> [options]"
        )
        print("\nOptions:")
        print("  --dry-run                    Only show what would be changed")
        print("  --test-user <username>       Only process specific user")
        print("  --debug                      Show detailed request/response information")
        print("\nExamples:")
        print(
            "  python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --dry-run"
        )
        print(
            "  python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --test-user robbert.uittenbroek --debug"
        )
        sys.exit(1)

    keycloak_url = args[0].rstrip("/")
    realm = args[1]
    admin_user = args[2]

    # Parse options
    dry_run = "--dry-run" in args
    debug = "--debug" in args
    test_user = None

    if "--test-user" in args:
        try:
            test_user_index = args.index("--test-user")
            test_user = args[test_user_index + 1]
        except (ValueError, IndexError):
            print("❌ Error: --test-user requires a username argument")
            sys.exit(1)

    # Prompt for password
    print(f"Keycloak URL: {keycloak_url}")
    print(f"Realm: {realm}")
    print(f"Admin User: {admin_user}")
    if dry_run:
        print("Mode: 🔍 DRY-RUN (no changes will be made)")
    if debug:
        print("Mode: 🐛 DEBUG (detailed request/response logging)")
    if test_user:
        print(f"Test User: {test_user}")
    print()
    admin_password = getpass.getpass("Admin Password: ")

    # Get admin token
    print("\n🔐 Authenticating...")
    try:
        token = get_admin_token(keycloak_url, admin_user, admin_password)
    except requests.exceptions.HTTPError as e:
        print(f"❌ Authentication failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

    print("✅ Authentication successful")

    # Fetch users
    if test_user:
        print(f"\n👤 Fetching user '{test_user}'...")
        try:
            user = get_user_by_username(keycloak_url, realm, token, test_user)
            if not user:
                print(f"❌ User '{test_user}' not found")
                sys.exit(1)
            users = [user]
        except Exception as e:
            print(f"❌ Failed to fetch user: {e}")
            sys.exit(1)
    else:
        print(f"\n👥 Fetching all users from realm '{realm}'...")
        try:
            users = get_users(keycloak_url, realm, token)
        except Exception as e:
            print(f"❌ Failed to fetch users: {e}")
            sys.exit(1)

    print(f"Found {len(users)} user(s)\n")

    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE - No changes will be made")
        print("=" * 60)
        print()

    # Process users
    updated_count = 0
    skipped_count = 0
    error_count = 0
    details_list = []

    for i, user in enumerate(users, 1):
        username = user.get("username", user.get("id", "unknown"))
        email = user.get("email", "no-email")

        if len(users) > 1:
            print(f"[{i}/{len(users)}] {username} ({email})...", end=" ")
        else:
            print(f"Processing {username} ({email})...")

        success, message, details = backfill_user(
            keycloak_url, realm, token, user, dry_run, debug
        )

        if details:
            details["username"] = username
            details["email"] = email
            details_list.append(details)

        if success:
            print(f"✅ {message}")
            updated_count += 1
        else:
            if "Already has" in message or "No sso-rijk" in message:
                print(f"⏭️  {message}")
                skipped_count += 1
            else:
                print(f"❌ {message}")
                error_count += 1

    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    if dry_run:
        print(f"  🔍 Would update: {updated_count}")
    else:
        print(f"  ✅ Updated: {updated_count}")
    print(f"  ⏭️  Skipped: {skipped_count}")
    print(f"  ❌ Errors:  {error_count}")
    print(f"  📊 Total:   {len(users)}")
    print("=" * 60)

    # Show detailed analysis for test user
    if test_user and details_list:
        print("\nDetailed Analysis:")
        print("=" * 60)
        for detail in details_list:
            print(f"Username: {detail['username']}")
            print(f"Email: {detail['email']}")
            print(f"SSO-Rijk userId (from NameID): {detail['sso_userid']}")
            print(f"SSO-Rijk userName: {detail['sso_username']}")
            if "existing_original" in detail:
                print(f"Existing sso-rijk-userid: {detail['existing_original']}")
                print(f"Existing sso-rijk-userid-lowercase: {detail['existing_lowercase']}")
            if detail["would_set"]:
                action = "Would be set" if dry_run else "Was set"
                print(f"Action: {action}")
                print(f"  - sso-rijk-userid = {detail['sso_userid']}")
                print(f"  - sso-rijk-userid-lowercase = {detail['sso_userid_lowercase']}")
            print("=" * 60)

    if dry_run:
        print("\nℹ️  This was a dry-run. No changes were made.")
        print("💡 Run without --dry-run to apply changes.")
    elif updated_count > 0:
        print(f"\n✅ Successfully backfilled {updated_count} user(s)")
    else:
        print("\nℹ️  No users were updated")


if __name__ == "__main__":
    main()
