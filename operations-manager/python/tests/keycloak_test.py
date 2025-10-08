#!/usr/bin/env python3
"""
Minimal Keycloak OIDC test using provided credentials.

Tests basic OIDC connectivity and token exchange using:
- OIDC_CLIENT_ID: wies
- OIDC_CLIENT_SECRET: WKB6KHSWw7BegeoY2K9cYbuFPF9QnG77
- OIDC_DISCOVERY_URL: https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration
"""

import asyncio
import logging

import httpx
import pytest

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Test credentials
OIDC_CLIENT_ID = "wies"
OIDC_CLIENT_SECRET = "WKB6KHSWw7BegeoY2K9cYbuFPF9QnG77"
OIDC_DISCOVERY_URL = "https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration"


@pytest.mark.asyncio
async def test_discovery_endpoint():
    """Test OIDC discovery endpoint connectivity."""
    logger.info("Testing OIDC discovery endpoint...")

    async with httpx.AsyncClient() as client:
        response = await client.get(OIDC_DISCOVERY_URL)
        response.raise_for_status()

        discovery_data = response.json()

        # Verify required OIDC discovery fields
        required_fields = ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"]

        missing_fields = [field for field in required_fields if field not in discovery_data]
        assert not missing_fields, f"Missing required OIDC discovery fields: {missing_fields}"

        logger.info("✅ OIDC discovery endpoint accessible")
        logger.info(f"Issuer: {discovery_data['issuer']}")
        logger.info(f"Token endpoint: {discovery_data['token_endpoint']}")
        logger.info(f"Userinfo endpoint: {discovery_data['userinfo_endpoint']}")

        return discovery_data


@pytest.mark.asyncio
async def test_client_credentials_flow():
    """Test client credentials flow with provided client ID and secret."""
    logger.info("Testing client credentials flow...")

    # Get discovery data directly
    async with httpx.AsyncClient() as client:
        response = await client.get(OIDC_DISCOVERY_URL)
        response.raise_for_status()
        discovery_data = response.json()

    token_endpoint = discovery_data["token_endpoint"]

    async with httpx.AsyncClient() as client:
        # Attempt client credentials grant
        token_data = {
            "grant_type": "client_credentials",
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
        }

        response = await client.post(
            token_endpoint, data=token_data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        response.raise_for_status()
        token_response = response.json()

        assert "access_token" in token_response, "Access token not present in response"
        assert token_response.get("token_type") == "Bearer", "Token type should be Bearer"

        logger.info("✅ Client credentials flow successful")
        logger.info(f"Token type: {token_response.get('token_type', 'N/A')}")
        logger.info(f"Expires in: {token_response.get('expires_in', 'N/A')} seconds")

        return token_response


@pytest.mark.asyncio
async def test_userinfo_endpoint_access():
    """Test userinfo endpoint accessibility (without authentication)."""
    logger.info("Testing userinfo endpoint accessibility...")

    # Get discovery data directly
    async with httpx.AsyncClient() as client:
        response = await client.get(OIDC_DISCOVERY_URL)
        response.raise_for_status()
        discovery_data = response.json()

    userinfo_endpoint = discovery_data["userinfo_endpoint"]

    async with httpx.AsyncClient() as client:
        # Test endpoint accessibility (should return 401 without auth)
        response = await client.get(userinfo_endpoint)

        assert response.status_code == 401, f"Expected 401 Unauthorized, got {response.status_code}"

        logger.info("✅ Userinfo endpoint accessible (correctly returns 401 without auth)")
        return True


async def run_all_tests():
    """Run all Keycloak connectivity tests."""
    logger.info("=== Starting Keycloak OIDC Connectivity Tests ===")

    tests = [
        ("Discovery Endpoint", test_discovery_endpoint),
        ("Client Credentials Flow", test_client_credentials_flow),
        ("Userinfo Endpoint Access", test_userinfo_endpoint_access),
    ]

    results = {}

    for test_name, test_func in tests:
        logger.info(f"\n--- Running: {test_name} ---")
        try:
            result = await test_func()
            results[test_name] = bool(result)
            logger.info(f"✅ {test_name} PASSED")
        except Exception as e:
            logger.error(f"❌ {test_name} ERROR: {e}")
            results[test_name] = False

    # Summary
    logger.info("\n=== Test Results Summary ===")
    passed = sum(1 for result in results.values() if result)
    total = len(results)

    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        logger.info(f"{test_name}: {status}")

    logger.info(f"\nOverall: {passed}/{total} tests passed")

    if passed == total:
        logger.info("🎉 All Keycloak connectivity tests passed!")
        return True
    else:
        logger.error("❌ Some Keycloak connectivity tests failed!")
        return False


if __name__ == "__main__":
    asyncio.run(run_all_tests())
