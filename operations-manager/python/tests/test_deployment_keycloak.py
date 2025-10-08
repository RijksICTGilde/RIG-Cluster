#!/usr/bin/env python3
"""
Test script for deployment-based Keycloak integration.

This script demonstrates the new shared realm approach where:
1. A default shared realm is created/ensured
2. Each deployment gets its own client in the shared realm
3. Clients are configured with specific ingress hosts
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


async def test_deployment_approach():
    """Test the new deployment-based approach."""
    logger.info("=== Testing Deployment-Based Keycloak Integration ===")

    try:
        # Create Keycloak connector
        connector = await create_keycloak_connector()
        logger.info("Created Keycloak connector successfully")

        # Step 1: Ensure default realm exists
        logger.info("Step 1: Ensuring default shared realm exists...")
        realm_info = await connector.ensure_default_realm_exists()
        logger.info(f"✅ Default realm ready: {realm_info['realm']}")

        # Step 2: Create deployment clients
        logger.info("Step 2: Creating clients for different deployments...")

        # Example deployment 1: Frontend app
        frontend_client = await connector.create_deployment_client(
            deployment_name="frontend",
            project_name="myapp",
            ingress_hosts=["myapp-frontend.apps.digilab.network", "myapp.example.com"],
        )

        logger.info("✅ Frontend client created:")
        logger.info(f"  Client ID: {frontend_client['client_id']}")
        logger.info(f"  Client Secret: {frontend_client['client_secret'][:10]}...")
        logger.info(f"  Discovery URL: {frontend_client['discovery_url']}")
        logger.info(f"  Ingress Hosts: {frontend_client['ingress_hosts']}")

        # Example deployment 2: API backend
        api_client = await connector.create_deployment_client(
            deployment_name="api", project_name="myapp", ingress_hosts=["myapp-api.apps.digilab.network"]
        )

        logger.info("✅ API client created:")
        logger.info(f"  Client ID: {api_client['client_id']}")
        logger.info(f"  Client Secret: {api_client['client_secret'][:10]}...")

        # Example deployment 3: Different project
        other_client = await connector.create_deployment_client(
            deployment_name="webapp",
            project_name="otherproject",
            ingress_hosts=["otherproject.apps.digilab.network", "other.example.com"],
        )

        logger.info("✅ Other project client created:")
        logger.info(f"  Client ID: {other_client['client_id']}")

        # Step 3: Verify all clients exist in shared realm
        logger.info("Step 3: Verifying all clients in shared realm...")
        from opi.core.config import settings

        realm_name = settings.KEYCLOAK_DEFAULT_REALM

        clients = await connector._api_request("GET", f"/{realm_name}/clients")
        deployment_clients = [
            c
            for c in clients
            if "-" in c["clientId"]
            and not c["clientId"].startswith(
                ("realm-management", "account", "admin-cli", "broker", "security-admin-console")
            )
        ]

        logger.info(f"✅ Found {len(deployment_clients)} deployment client(s) in shared realm:")
        for client in deployment_clients:
            logger.info(f"  - {client['clientId']} - Enabled: {client['enabled']}")

        # Step 4: Test updating hosts for a client
        logger.info("Step 4: Testing host updates...")
        await connector.update_deployment_client_hosts(
            deployment_name="frontend",
            project_name="myapp",
            ingress_hosts=["myapp-frontend.apps.digilab.network", "myapp.example.com", "myapp-new.example.com"],
        )
        logger.info("✅ Updated frontend client with new host")

        # Step 5: Generate example environment variables and secrets
        logger.info("Step 5: Generating deployment configuration examples...")

        print("\n" + "=" * 60)
        print("DEPLOYMENT CONFIGURATION EXAMPLES")
        print("=" * 60)

        print("\n📝 Environment Variables for myapp-frontend deployment:")
        print(f"OIDC_CLIENT_ID={frontend_client['client_id']}")
        print(f"OIDC_CLIENT_SECRET={frontend_client['client_secret']}")
        print(f"OIDC_DISCOVERY_URL={frontend_client['discovery_url']}")

        print("\n📝 Environment Variables for myapp-api deployment:")
        print(f"OIDC_CLIENT_ID={api_client['client_id']}")
        print(f"OIDC_CLIENT_SECRET={api_client['client_secret']}")
        print(f"OIDC_DISCOVERY_URL={api_client['discovery_url']}")

        print("\n📝 Example SOPS-encrypted secret YAML:")
        print(f"""apiVersion: v1
kind: Secret
metadata:
  name: myapp-frontend-oidc
  namespace: myapp
type: Opaque
stringData:
  OIDC_CLIENT_ID: {frontend_client['client_id']}
  OIDC_CLIENT_SECRET: ENC[AES256_GCM,data:abc123...,iv:def456...,tag:ghi789...,type:str]
  OIDC_DISCOVERY_URL: {frontend_client['discovery_url']}""")

        print("\n📝 Example values.yaml section:")
        print(f"""oidc:
  enabled: true
  clientId: {frontend_client['client_id']}
  clientSecretRef:
    name: myapp-frontend-oidc
    key: OIDC_CLIENT_SECRET
  discoveryUrl: {frontend_client['discovery_url']}
  
ingress:
  hosts:
    - host: myapp-frontend.apps.digilab.network
      paths: ["/"]
    - host: myapp.example.com
      paths: ["/"]""")

        print("\n" + "=" * 60)

        # Ask about cleanup
        print("\nTest completed successfully! 🎉")
        response = input("\nDo you want to clean up the test clients? (y/N): ")

        if response.lower() in ("y", "yes"):
            logger.info("Step 6: Cleaning up test clients...")

            await connector.delete_deployment_client("frontend", "myapp")
            logger.info("✅ Deleted frontend client")

            await connector.delete_deployment_client("api", "myapp")
            logger.info("✅ Deleted API client")

            await connector.delete_deployment_client("webapp", "otherproject")
            logger.info("✅ Deleted other project client")

            logger.info("✅ All test clients cleaned up")
        else:
            logger.info("Test clients left intact for inspection")
            logger.info(f"You can view them at: http://keycloak.kind/admin/master/console/#/{realm_name}/clients")

        return True

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


if __name__ == "__main__":
    asyncio.run(test_deployment_approach())
