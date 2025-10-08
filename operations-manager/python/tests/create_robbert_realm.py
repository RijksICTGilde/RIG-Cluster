#!/usr/bin/env python3
"""
Create and keep the 'robbert' realm for inspection.
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


async def create_robbert_realm():
    """Create the robbert realm and keep it."""
    logger.info("=== Creating 'robbert' realm (keeping it) ===")

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
        logger.info("Verifying realm configuration...")
        realm_info = await connector._api_request("GET", f"/{realm_name}")
        logger.info(f"✅ Verified realm exists: {realm_info['realm']}")

        # Check identity providers
        providers = await connector._api_request("GET", f"/{realm_name}/identity-provider/instances")
        if providers:
            logger.info(f"✅ Found {len(providers)} identity provider(s):")
            for provider in providers:
                logger.info(f"  - {provider['alias']} ({provider['providerId']}) - Enabled: {provider['enabled']}")
                logger.info(f"    Authenticate by default: {provider.get('authenticateByDefault', False)}")
                logger.info(f"    Client ID: {provider.get('config', {}).get('clientId', 'N/A')}")

        # Check clients
        clients = await connector._api_request("GET", f"/{realm_name}/clients")
        if clients:
            custom_clients = [
                c
                for c in clients
                if not c["clientId"].startswith(
                    ("realm-management", "account", "admin-cli", "broker", "security-admin-console")
                )
            ]
            logger.info(f"✅ Found {len(custom_clients)} custom client(s):")
            for client in custom_clients:
                logger.info(f"  - {client['clientId']} - Enabled: {client['enabled']}")

        logger.info("=== Realm 'robbert' created and ready for inspection! ===")
        logger.info("You can view it at: http://keycloak.kind/admin/master/console/#/robbert")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to create realm: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


if __name__ == "__main__":
    asyncio.run(create_robbert_realm())
