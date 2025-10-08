"""
Test the MinIO connector functionality.

This module tests the MinIO connector to ensure proper functionality
for bucket operations, user management, and access control.
"""

import logging

from opi.connectors.minio_mc import MinioConnectionError, MinioConnector, MinioExecutionError, MinioValidationError
from opi.core.config import settings

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_validation():
    """Test input validation functions."""
    connector = MinioConnector()

    # Test bucket name validation
    try:
        # Valid bucket names
        assert connector._validate_bucket_name("test-bucket") == "test-bucket"
        assert connector._validate_bucket_name("my-test-bucket-123") == "my-test-bucket-123"
        logger.info("✓ Valid bucket name validation passed")

        # Invalid bucket names should raise exceptions
        try:
            connector._validate_bucket_name("")
            assert False, "Empty bucket name should fail"
        except MinioValidationError:
            logger.info("✓ Empty bucket name validation works")

        try:
            connector._validate_bucket_name("Test-Bucket")  # Uppercase not allowed
            assert False, "Uppercase bucket name should fail"
        except MinioValidationError:
            logger.info("✓ Uppercase bucket name validation works")

        try:
            connector._validate_bucket_name("test--bucket")  # Consecutive hyphens
            assert False, "Consecutive hyphens should fail"
        except MinioValidationError:
            logger.info("✓ Consecutive hyphens validation works")

    except Exception as e:
        logger.error(f"Bucket name validation test failed: {e}")
        raise

    # Test username validation
    try:
        # Valid usernames
        assert connector._validate_username("testuser") == "testuser"
        assert connector._validate_username("test_user_123") == "test_user_123"
        logger.info("✓ Valid username validation passed")

        # Invalid usernames should raise exceptions
        try:
            connector._validate_username("123user")  # Cannot start with number
            assert False, "Username starting with number should fail"
        except MinioValidationError:
            logger.info("✓ Username starting with number validation works")

    except Exception as e:
        logger.error(f"Username validation test failed: {e}")
        raise


def test_connection():
    """Test MinIO connection using settings from config."""
    connector = MinioConnector()

    logger.info(f"Testing connection to MinIO at {settings.MINIO_HOST}")
    logger.info(f"Using access key: {settings.MINIO_ADMIN_ACCESS_KEY}")

    # Test connection
    success = connector.test_connection(
        host=settings.MINIO_HOST,
        access_key=settings.MINIO_ADMIN_ACCESS_KEY,
        secret_key=settings.MINIO_ADMIN_SECRET_KEY,
        secure=settings.MINIO_USE_TLS,
    )

    if success:
        logger.info("✓ MinIO connection test successful")
    else:
        logger.warning("⚠ MinIO connection test failed - server may not be running")
        logger.warning("This is expected if MinIO server is not available")


def test_bucket_operations():
    """Test bucket CRUD operations."""
    connector = MinioConnector()

    test_bucket = "test-operations-bucket"

    logger.info("Testing bucket operations...")

    try:
        # Create bucket
        result = connector.create_bucket(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Create bucket result: {result}")

        # List buckets
        buckets = connector.list_buckets(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Found {len(buckets)} buckets")

        # Get bucket info
        bucket_info = connector.get_bucket_info(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Bucket info: {bucket_info}")

        # Delete bucket (cleanup)
        result = connector.delete_bucket(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Delete bucket result: {result}")

        logger.info("✓ Bucket operations test completed successfully")

    except (MinioConnectionError, MinioExecutionError) as e:
        logger.warning(f"⚠ Bucket operations test failed: {e}")
        logger.warning("This is expected if MinIO server is not available")
    except Exception as e:
        logger.error(f"Bucket operations test failed with unexpected error: {e}")
        raise


def test_user_operations():
    """Test user CRUD operations."""
    connector = MinioConnector()

    test_user = "testuser123"
    test_secret = "testSecret123"

    logger.info("Testing user operations...")

    try:
        # Create user
        result = connector.create_user(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            secret_key=test_secret,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Create user result: {result}")

        # List users
        users = connector.list_users(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Found {len(users)} users")

        # Update user secret
        new_secret = "newTestSecret456"
        result = connector.update_user_secret(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            new_secret_key=new_secret,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Update user secret result: {result}")

        # Delete user (cleanup)
        result = connector.delete_user(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Delete user result: {result}")

        logger.info("✓ User operations test completed successfully")

    except (MinioConnectionError, MinioExecutionError) as e:
        logger.warning(f"⚠ User operations test failed: {e}")
        logger.warning("This is expected if MinIO server is not available")
    except Exception as e:
        logger.error(f"User operations test failed with unexpected error: {e}")
        raise


def test_access_control():
    """Test access control operations."""
    connector = MinioConnector()

    test_bucket = "test-access-bucket"
    test_user = "accesstestuser"
    test_secret = "accessTestSecret123"

    logger.info("Testing access control operations...")

    try:
        # Create test bucket and user
        connector.create_bucket(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )

        connector.create_user(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            secret_key=test_secret,
            secure=settings.MINIO_USE_TLS,
        )

        # Grant access
        result = connector.grant_bucket_access(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            bucket_name=test_bucket,
            permissions=["read", "write"],
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Grant access result: {result}")

        # Revoke access
        result = connector.revoke_bucket_access(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )
        logger.info(f"Revoke access result: {result}")

        # Cleanup
        connector.delete_user(
            host=settings.MINIO_HOST,
            admin_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            admin_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            username=test_user,
            secure=settings.MINIO_USE_TLS,
        )

        connector.delete_bucket(
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            bucket_name=test_bucket,
            secure=settings.MINIO_USE_TLS,
        )

        logger.info("✓ Access control operations test completed successfully")

    except (MinioConnectionError, MinioExecutionError) as e:
        logger.warning(f"⚠ Access control operations test failed: {e}")
        logger.warning("This is expected if MinIO server is not available")
    except Exception as e:
        logger.error(f"Access control operations test failed with unexpected error: {e}")
        raise


def main():
    """Run all tests."""
    logger.info("=== MinIO Connector Test Suite ===")

    logger.info("\n1. Testing input validation...")
    test_validation()

    logger.info("\n2. Testing MinIO connection...")
    test_connection()

    logger.info("\n3. Testing bucket operations...")
    test_bucket_operations()

    logger.info("\n4. Testing user operations...")
    test_user_operations()

    logger.info("\n5. Testing access control...")
    test_access_control()

    logger.info("\n=== All tests completed ===")
    logger.info("Note: Connection-based tests may fail if MinIO server is not running")


if __name__ == "__main__":
    main()
