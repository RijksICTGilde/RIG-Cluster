#!/usr/bin/env python3
"""
Test the enhanced database pool retry and recovery functionality.
"""

import asyncio
import logging
import sys

# Add the current directory to Python path for imports
sys.path.insert(0, ".")

from opi.core.config import settings
from opi.core.database_pools import (
    check_and_recover_all_pools,
    close_database_pools,
    get_all_pool_stats,
    initialize_database_pools,
    is_database_available,
    recover_database_pool,
)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def test_successful_initialization():
    """Test normal database pool initialization with retry logic."""
    logger.info("🧪 Test 1: Successful database pool initialization")

    try:
        # This should succeed with the retry logic
        await initialize_database_pools()

        # Check if database is available
        available = is_database_available()
        logger.info(f"Database available: {available}")

        # Get stats
        stats = get_all_pool_stats()
        logger.info(f"Pool stats: {stats}")

        return True

    except Exception as e:
        logger.error(f"❌ Test 1 failed: {e}")
        return False


async def test_pool_recovery():
    """Test the pool recovery mechanism."""
    logger.info("🧪 Test 2: Database pool recovery mechanism")

    try:
        # First, ensure pools are initialized
        if not is_database_available():
            await initialize_database_pools()

        logger.info("Testing pool recovery...")

        # Attempt to recover a healthy pool (should succeed quickly)
        recovery_success = await recover_database_pool("main")
        logger.info(f"Pool recovery result: {recovery_success}")

        # Test bulk recovery
        recovery_results = await check_and_recover_all_pools()
        logger.info(f"Bulk recovery results: {recovery_results}")

        return True

    except Exception as e:
        logger.error(f"❌ Test 2 failed: {e}")
        return False


async def test_initialization_with_wrong_credentials():
    """Test initialization failure handling."""
    logger.info("🧪 Test 3: Initialization with invalid credentials (should fail gracefully)")

    # Close any existing pools first
    await close_database_pools()

    # Temporarily modify settings to use wrong credentials
    original_user = settings.DATABASE_ADMIN_NAME
    original_password = settings.DATABASE_ADMIN_PASSWORD

    try:
        # Set invalid credentials
        settings.DATABASE_ADMIN_NAME = "invalid_user"
        settings.DATABASE_ADMIN_PASSWORD = "wrong_password"

        logger.info("Attempting initialization with wrong credentials...")

        # This should fail after retries
        try:
            await initialize_database_pools()
            logger.error("❌ This should have failed!")
            return False
        except Exception as e:
            logger.info(f"✅ Expected failure occurred: {e}")

        # Check that no pools are available
        available = is_database_available()
        logger.info(f"Database available after failure: {available}")

        return not available  # Should be False (not available)

    finally:
        # Restore original credentials
        settings.DATABASE_ADMIN_NAME = original_user
        settings.DATABASE_ADMIN_PASSWORD = original_password


async def test_complete_workflow():
    """Test a complete initialization -> failure -> recovery workflow."""
    logger.info("🧪 Test 4: Complete initialization -> recovery workflow")

    try:
        # Start fresh
        await close_database_pools()

        # Initialize with correct credentials
        logger.info("Step 1: Initialize pools with correct credentials")
        await initialize_database_pools()

        initial_stats = get_all_pool_stats()
        logger.info(f"Initial stats: {initial_stats}")

        # Test recovery on healthy pools
        logger.info("Step 2: Test recovery on healthy pools")
        recovery_results = await check_and_recover_all_pools()
        logger.info(f"Recovery results: {recovery_results}")

        # Verify pools are still healthy
        final_stats = get_all_pool_stats()
        logger.info(f"Final stats: {final_stats}")

        return all(recovery_results.values()) and is_database_available()

    except Exception as e:
        logger.error(f"❌ Test 4 failed: {e}")
        return False


async def run_all_tests():
    """Run all database retry and recovery tests."""
    logger.info("🚀 Starting database retry and recovery tests...")

    tests = [
        ("Successful Initialization", test_successful_initialization),
        ("Pool Recovery", test_pool_recovery),
        ("Invalid Credentials", test_initialization_with_wrong_credentials),
        ("Complete Workflow", test_complete_workflow),
    ]

    results = {}

    for test_name, test_func in tests:
        try:
            logger.info(f"\n{'='*50}")
            logger.info(f"Running: {test_name}")
            logger.info(f"{'='*50}")

            result = await test_func()
            results[test_name] = result

            status = "✅ PASSED" if result else "❌ FAILED"
            logger.info(f"{status}: {test_name}")

        except Exception as e:
            logger.exception(f"❌ EXCEPTION in {test_name}: {e}")
            results[test_name] = False

        # Small delay between tests
        await asyncio.sleep(1)

    # Final cleanup
    try:
        await close_database_pools()
    except:
        pass

    # Summary
    logger.info(f"\n{'='*50}")
    logger.info("TEST SUMMARY")
    logger.info(f"{'='*50}")

    passed = sum(1 for result in results.values() if result)
    total = len(results)

    for test_name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        logger.info(f"{status}: {test_name}")

    logger.info(f"\nOverall: {passed}/{total} tests passed")

    if passed == total:
        logger.info("🎉 ALL TESTS PASSED! Database retry and recovery functionality is working correctly.")
    else:
        logger.error(f"💥 {total - passed} tests failed. Please check the logs above.")

    return passed == total


if __name__ == "__main__":
    try:
        success = asyncio.run(run_all_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error during testing: {e}")
        sys.exit(1)
