"""
Keycloak client credential startup logic for the operations manager.

This module handles ensuring the operations manager has valid OIDC credentials
during startup by checking existing credentials and creating/retrieving them
from Keycloak if needed.
"""

import logging

import httpx

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.config import settings

logger = logging.getLogger(__name__)


async def validate_oidc_credentials() -> bool:
    """
    Validate existing OIDC credentials by attempting to get a token.

    Returns:
        True if credentials are valid and working, False otherwise
    """
    if not (settings.OIDC_CLIENT_ID and settings.OIDC_CLIENT_SECRET and settings.OIDC_DISCOVERY_URL):
        logger.debug("OIDC credentials not fully configured")
        return False

    logger.info(f"Validating existing OIDC credentials: OIDC_CLIENT_ID = {settings.OIDC_CLIENT_ID}")
    logger.info(f"Validating existing OIDC credentials: OIDC_OIDC_DISCOVERY_URL = {settings.OIDC_DISCOVERY_URL}")
    logger.info(f"Validating existing OIDC credentials: OIDC_CLIENT_SECRET = {settings.OIDC_CLIENT_SECRET[:4]}***")

    try:
        from opi.bootstrap.keycloak_setup import extract_realm_from_discovery_url

        realm_name = extract_realm_from_discovery_url(settings.OIDC_DISCOVERY_URL)
        if not realm_name:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM
        token_url = f"{settings.KEYCLOAK_URL}/realms/{realm_name}/protocol/openid-connect/token"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.OIDC_CLIENT_ID,
                    "client_secret": settings.OIDC_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )

            if response.status_code == 200:
                logger.info("OIDC credentials validated successfully")
                return True
            else:
                logger.warning(f"OIDC credential validation failed with status {response.status_code}: {response.text}")
                return False

    except Exception as e:
        logger.warning(f"Error validating OIDC credentials: {e}")
        return False


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
