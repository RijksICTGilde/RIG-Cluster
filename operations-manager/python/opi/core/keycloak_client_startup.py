"""
Keycloak client credential startup logic for the operations manager.

This module handles ensuring the operations manager has valid OIDC credentials
during startup by checking existing credentials and creating/retrieving them
from Keycloak if needed.
"""

import logging

from opi.bootstrap.keycloak_setup import extract_realm_from_discovery_url
from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.config import settings

logger = logging.getLogger(__name__)


async def validate_oidc_credentials() -> bool:
    """
    Check that OPI's client secret is the one Keycloak holds.

    Compares through the admin API instead of requesting a token: since RC-325 a new client
    has no service account, so a client_credentials grant is refused whatever the secret.

    Returns:
        True if the client exists with this secret, False otherwise
    """
    if not (settings.OIDC_CLIENT_ID and settings.OIDC_CLIENT_SECRET and settings.OIDC_DISCOVERY_URL):
        logger.debug("OIDC credentials not fully configured")
        return False

    realm_name = extract_realm_from_discovery_url(settings.OIDC_DISCOVERY_URL) or settings.KEYCLOAK_DEFAULT_REALM
    logger.info(f"Validating OIDC client '{settings.OIDC_CLIENT_ID}' in realm '{realm_name}'")

    keycloak = await create_keycloak_connector()
    client = await keycloak.find_client_by_client_id(settings.OIDC_CLIENT_ID, realm_name)
    if not client:
        logger.warning(f"OIDC client '{settings.OIDC_CLIENT_ID}' not found in realm '{realm_name}'")
        return False

    if await keycloak.get_client_secret(client["id"], realm_name) != settings.OIDC_CLIENT_SECRET:
        logger.warning(f"OIDC client '{settings.OIDC_CLIENT_ID}' has a different secret in realm '{realm_name}'")
        return False

    logger.info("OIDC credentials validated successfully")
    return True


async def ensure_keycloak_credentials() -> bool:
    """
    Ensure that the operations manager has valid Keycloak credentials.

    This function implements the complete credential management flow:
    1. Check if OIDC environment variables are set and validate them
    2. If missing or invalid, retrieve/create credentials from Keycloak
    3. Update the Kubernetes secret if credentials were changed
    4. Update current process environment variables

    Returns:
        True if valid credentials are available, False if setup failed
    """
    logger.info("Ensuring Keycloak credentials for operations manager")

    # Phase 1: Check existing credentials
    if await validate_oidc_credentials():
        logger.info("Existing OIDC credentials are valid - no action needed")
        return True

    logger.info("OIDC credentials missing or invalid - attempting recovery/creation")

    # Phase 2: Get or create credentials using existing Keycloak setup logic
    try:
        from opi.bootstrap.keycloak_setup import OPERATIONS_REALM_NAME, KeycloakSetup

        keycloak_setup = KeycloakSetup()

        # Initialize connectors (same as in KeycloakSetup)
        keycloak_setup.keycloak = await create_keycloak_connector()
        keycloak_setup.kubectl = KubectlConnector()

        # Recovery: always create in OPI's own realm
        success = await keycloak_setup.setup_operations_client(realm_name=OPERATIONS_REALM_NAME)

        if success:
            # Phase 3: Final validation
            if await validate_oidc_credentials():
                logger.info("Keycloak credentials successfully ensured and validated")
                return True
            else:
                logger.error("Credential setup completed but validation still fails")
                return False
        else:
            logger.error("Failed to setup operations client credentials")
            return False

    except Exception as e:
        logger.error(f"Error ensuring Keycloak credentials: {e}")
        return False
