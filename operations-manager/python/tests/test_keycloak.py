#!/usr/bin/env python3
"""
Test script for Keycloak API integration.

This script tests creating a realm called 'robbert' with master OIDC provider
configured as the default and only authentication method.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from opi.connectors.keycloak import create_keycloak_connector

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def test_create_realm():
    """Test creating a realm with OIDC provider."""
    logger.info("=== Testing Keycloak Realm Creation ===")

    try:
        # Create Keycloak connector using config defaults
        connector = await create_keycloak_connector()
        logger.info("Created Keycloak connector successfully")

        # Test realm name
        realm_name = "robbert"

        # Check if realm already exists and delete it
        try:
            logger.info(f"Checking if realm '{realm_name}' already exists...")
            existing_realm = await connector._api_request("GET", f"/{realm_name}")
            if existing_realm:
                logger.info(f"Realm '{realm_name}' already exists, deleting it first...")
                await connector.delete_realm(realm_name)
                logger.info(f"Deleted existing realm '{realm_name}'")
        except Exception as e:
            logger.info(f"Realm '{realm_name}' does not exist (this is expected): {e}")

        # Create the new realm
        logger.info(f"Creating realm '{realm_name}'...")
        result = await connector.create_realm(realm_name=realm_name, display_name="Robbert Test Realm")

        logger.info("✅ Realm creation successful!")
        logger.info(f"Realm info: {result['realm']['realm']}")
        logger.info(f"Client ID: {result['client_id']}")
        logger.info(f"Client Secret: {result['client_secret'][:10]}...")  # Only show first 10 chars
        logger.info(f"Discovery URL: {result['discovery_url']}")

        # Verify the realm exists
        logger.info("Verifying realm was created...")
        realm_info = await connector._api_request("GET", f"/{realm_name}")
        logger.info(f"✅ Verified realm exists: {realm_info['realm']}")

        # Check identity providers
        logger.info("Checking identity providers...")
        providers = await connector._api_request("GET", f"/{realm_name}/identity-provider/instances")
        if providers:
            logger.info(f"✅ Found {len(providers)} identity provider(s):")
            for provider in providers:
                logger.info(f"  - {provider['alias']} ({provider['providerId']}) - Enabled: {provider['enabled']}")
                logger.info(f"    Authenticate by default: {provider.get('authenticateByDefault', False)}")
        else:
            logger.warning("⚠️  No identity providers found")

        # Check clients
        logger.info("Checking clients...")
        clients = await connector._api_request("GET", f"/{realm_name}/clients")
        if clients:
            logger.info(f"✅ Found {len(clients)} client(s):")
            for client in clients:
                if not client["clientId"].startswith("realm-management") and not client["clientId"].startswith(
                    "account"
                ):
                    logger.info(f"  - {client['clientId']} - Enabled: {client['enabled']}")

        logger.info("=== Test completed successfully! ===")
        return True

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


async def test_cleanup():
    """Clean up test realm."""
    logger.info("=== Cleaning up test realm ===")

    try:
        connector = await create_keycloak_connector()
        await connector.delete_realm("robbert")
        logger.info("✅ Test realm 'robbert' deleted successfully")
    except Exception as e:
        logger.warning(f"⚠️  Could not delete test realm: {e}")


async def main():
    """Main test function."""
    print("\n🧪 Starting Keycloak API Test\n")

    success = await test_create_realm()

    if success:
        print("\n🎉 All tests passed!")

        # Ask if user wants to clean up
        response = input("\nDo you want to delete the test realm 'robbert'? (y/N): ")
        if response.lower() in ("y", "yes"):
            await test_cleanup()
        else:
            print("Test realm 'robbert' left intact for inspection.")
    else:
        print("\n❌ Tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
