#!/usr/bin/env python3
"""
Script to set up custom client scopes for Keycloak clients.

This script automates the setup of custom attribute passthrough for organization data.
"""

import asyncio
import logging
import sys
from typing import Any

from opi.connectors.keycloak import create_keycloak_connector
from opi.core.config import settings

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def setup_client_scope_for_realm(realm_name: str, client_id: str | None = None) -> bool:
    """
    Set up custom client scope for a specific realm and optionally assign to client.

    Args:
        realm_name: Name of the Keycloak realm
        client_id: Client ID to assign the scope to (optional)

    Returns:
        True if setup was successful
    """
    try:
        # Create Keycloak connector
        connector = create_keycloak_connector()

        logger.info(f"Setting up custom client scope for realm '{realm_name}'")

        if client_id:
            # Full setup including client assignment
            success = await connector.setup_custom_attributes_for_client(
                realm_name=realm_name,
                client_id=client_id,
                attributes=["organization.name", "organization.number", "organization.role"],
            )

            if success:
                logger.info(
                    f"✅ Successfully set up custom attributes for client '{client_id}' in realm '{realm_name}'"
                )
            else:
                logger.error(f"❌ Failed to set up custom attributes for client '{client_id}'")

            return success
        else:
            # Just create the client scope
            client_scope = await connector.create_custom_client_scope(realm_name)

            if not client_scope:
                logger.error(f"❌ Failed to create custom client scope in realm '{realm_name}'")
                return False

            scope_id = client_scope["id"]
            logger.info(f"✅ Created client scope '{client_scope['name']}' with ID: {scope_id}")

            # Add attribute mappers
            attributes = ["organization.name", "organization.number", "organization.role"]
            for attribute in attributes:
                success = await connector.add_attribute_mapper_to_scope(realm_name, scope_id, attribute)
                if success:
                    logger.info(f"✅ Added mapper for attribute '{attribute}'")
                else:
                    logger.warning(f"⚠️ Failed to add mapper for attribute '{attribute}'")

            logger.info(f"✅ Client scope setup completed for realm '{realm_name}'")
            logger.info("💡 To assign this scope to a client, use: --client-id <client_id>")
            return True

    except Exception as e:
        logger.error(f"❌ Error setting up client scope: {e}")
        return False


async def list_existing_client_scopes(realm_name: str) -> list[dict[str, Any]]:
    """
    List existing client scopes in a realm.

    Args:
        realm_name: Name of the Keycloak realm

    Returns:
        List of client scopes
    """
    try:
        connector = create_keycloak_connector()

        scopes = await connector._api_request("GET", f"/admin/realms/{realm_name}/client-scopes")

        logger.info(f"Client scopes in realm '{realm_name}':")
        for scope in scopes:
            logger.info(f"  - {scope['name']} (ID: {scope['id']})")

        return scopes

    except Exception as e:
        logger.error(f"❌ Error listing client scopes: {e}")
        return []


async def main():
    """Main entry point for the script."""
    import argparse

    parser = argparse.ArgumentParser(description="Set up Keycloak custom client scopes for organization attributes")
    parser.add_argument("--realm", required=True, help="Keycloak realm name (e.g., 'rig-platform')")
    parser.add_argument("--client-id", help="Client ID to assign the scope to (optional)")
    parser.add_argument("--list", action="store_true", help="List existing client scopes instead of creating")

    args = parser.parse_args()

    # Validate configuration
    if not settings.KEYCLOAK_URL:
        logger.error("❌ KEYCLOAK_URL not configured")
        sys.exit(1)

    if not settings.KEYCLOAK_ADMIN_USERNAME or not settings.KEYCLOAK_ADMIN_PASSWORD:
        logger.error("❌ Keycloak admin credentials not configured")
        sys.exit(1)

    logger.info(f"🔗 Connecting to Keycloak at: {settings.KEYCLOAK_URL}")
    logger.info(f"👤 Using admin user: {settings.KEYCLOAK_ADMIN_USERNAME}")

    if args.list:
        # List existing client scopes
        scopes = await list_existing_client_scopes(args.realm)

        # Check if custom scope already exists
        custom_scope_exists = any(scope["name"] == "custom_attributes_passthrough" for scope in scopes)
        if custom_scope_exists:
            logger.info("✅ Custom attributes passthrough scope already exists")
        else:
            logger.info("❌ Custom attributes passthrough scope not found")
            logger.info("💡 Run without --list to create the scope")

    else:
        # Set up client scope
        success = await setup_client_scope_for_realm(args.realm, args.client_id)

        if success:
            logger.info("🎉 Setup completed successfully!")
            logger.info("")
            logger.info("📋 Next steps:")
            logger.info("1. Verify the client scope in Keycloak Admin Console")
            logger.info("2. Add organization.* attributes to your users")
            logger.info("3. Test the SSO flow to verify attributes are passed")
            logger.info("")
            logger.info("📖 For manual setup instructions, see:")
            logger.info("   docs/KEYCLOAK_SETUP.md")
        else:
            logger.error("❌ Setup failed. Check logs for details.")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
