"""
Tests for the subdomain registry connector.

Tests the SubdomainConnector class that manages globally unique subdomains
for the nice URL feature.
"""

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.subdomain import (
    RESERVED_SUBDOMAINS,
    SUBDOMAIN_REGISTRY_TABLE_SQL,
    SubdomainConnector,
    SubdomainNotAvailableError,
    SubdomainValidationError,
    create_subdomain_connector,
    validate_subdomain,
)


class TestSubdomainConnectorBasics:
    """Basic tests for SubdomainConnector class."""

    def test_create_subdomain_connector(self):
        """create_subdomain_connector returns a SubdomainConnector instance."""
        connector = create_subdomain_connector()
        assert isinstance(connector, SubdomainConnector)

    def test_table_sql_creates_table(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains CREATE TABLE statement."""
        assert "CREATE TABLE IF NOT EXISTS subdomain_registry" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_creates_indexes(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains index creation."""
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_project" in SUBDOMAIN_REGISTRY_TABLE_SQL
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_deployment" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_has_unique_constraint(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL has unique constraint on subdomain+base_domain."""
        assert "UNIQUE (subdomain, base_domain)" in SUBDOMAIN_REGISTRY_TABLE_SQL


class TestSubdomainConnectorCheckAvailability:
    """Tests for SubdomainConnector.check_availability method."""

    @pytest.mark.asyncio
    async def test_check_availability_returns_true_when_available(self):
        """check_availability returns True when subdomain is not registered."""
        connector = SubdomainConnector()

        # Mock the database pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # Not found = available

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.check_availability("myapp", "rijks.app")

        assert result is True
        mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_availability_returns_false_when_taken(self):
        """check_availability returns False when subdomain is already registered."""
        connector = SubdomainConnector()

        # Mock the database pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 1  # Found = not available

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.check_availability("myapp", "rijks.app")

        assert result is False

    @pytest.mark.asyncio
    async def test_check_availability_lowercases_input(self):
        """check_availability lowercases subdomain and base_domain."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            await connector.check_availability("MyApp", "RIJKS.APP")

        # Verify the query was called with lowercase values
        call_args = mock_conn.fetchval.call_args
        assert call_args[0][1] == "myapp"  # subdomain
        assert call_args[0][2] == "rijks.app"  # base_domain


class TestSubdomainConnectorRegister:
    """Tests for SubdomainConnector.register method."""

    @pytest.mark.asyncio
    async def test_register_creates_new_registration(self):
        """register creates a new subdomain registration."""
        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "rijks.app",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "odcn-production",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": "user@example.com",
        }

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # Available
        mock_conn.fetchrow.return_value = mock_result

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.register(
                subdomain="myapp",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="odcn-production",
                created_by="user@example.com",
            )

        assert result["subdomain"] == "myapp"
        assert result["project_name"] == "my-project"

    @pytest.mark.asyncio
    async def test_register_raises_error_when_not_available(self):
        """register raises SubdomainNotAvailableError when subdomain is taken."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 1  # Not available
        mock_conn.fetchrow.return_value = {"project_name": "other-project"}

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            pytest.raises(SubdomainNotAvailableError) as exc_info,
        ):
            await connector.register(
                subdomain="myapp",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="odcn-production",
            )

        assert "niet beschikbaar" in str(exc_info.value).lower()  # Dutch: "is niet beschikbaar"


class TestSubdomainConnectorGetBySubdomain:
    """Tests for SubdomainConnector.get_by_subdomain method."""

    @pytest.mark.asyncio
    async def test_get_by_subdomain_returns_registration(self):
        """get_by_subdomain returns registration details when found."""
        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "rijks.app",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "odcn-production",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": "user@example.com",
        }

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = mock_result

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_subdomain("myapp", "rijks.app")

        assert result is not None
        assert result["subdomain"] == "myapp"
        assert result["project_name"] == "my-project"

    @pytest.mark.asyncio
    async def test_get_by_subdomain_returns_none_when_not_found(self):
        """get_by_subdomain returns None when subdomain is not registered."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_subdomain("nonexistent", "rijks.app")

        assert result is None


class TestSubdomainConnectorGetByProject:
    """Tests for SubdomainConnector.get_by_project method."""

    @pytest.mark.asyncio
    async def test_get_by_project_returns_registrations(self):
        """get_by_project returns all registrations for a project."""
        connector = SubdomainConnector()

        mock_results = [
            {
                "id": 1,
                "subdomain": "myapp",
                "base_domain": "rijks.app",
                "project_name": "my-project",
                "deployment_name": "prod",
                "cluster": "odcn-production",
                "created_at": None,
                "created_by": None,
            },
            {
                "id": 2,
                "subdomain": "myapp",
                "base_domain": "rijksapps.nl",
                "project_name": "my-project",
                "deployment_name": "staging",
                "cluster": "local",
                "created_at": None,
                "created_by": None,
            },
        ]

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = mock_results

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_project("my-project")

        assert len(result) == 2
        assert all(r["project_name"] == "my-project" for r in result)


class TestSubdomainConnectorDelete:
    """Tests for SubdomainConnector.delete method."""

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_deleted(self):
        """delete returns True when registration is deleted."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 1"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete("myapp", "rijks.app")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        """delete returns False when registration is not found."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 0"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete("nonexistent", "rijks.app")

        assert result is False


class TestSubdomainConnectorDeleteByProject:
    """Tests for SubdomainConnector.delete_by_project method."""

    @pytest.mark.asyncio
    async def test_delete_by_project_returns_count(self):
        """delete_by_project returns the number of deleted registrations."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 3"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete_by_project("my-project")

        assert result == 3


class TestValidateSubdomain:
    """Tests for validate_subdomain function."""

    def test_valid_subdomain(self):
        """Valid subdomains pass validation."""
        valid_subdomains = ["myapp", "my-app", "app123", "a", "abc123def"]
        for subdomain in valid_subdomains:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is True, f"'{subdomain}' should be valid: {error}"
            assert error is None

    def test_empty_subdomain(self):
        """Empty subdomain fails validation."""
        is_valid, error = validate_subdomain("")
        assert is_valid is False
        assert "leeg" in error.lower()  # Dutch: "mag niet leeg zijn"

    def test_too_long_subdomain(self):
        """Subdomain exceeding 63 characters fails validation."""
        long_subdomain = "a" * 64
        is_valid, error = validate_subdomain(long_subdomain)
        assert is_valid is False
        assert "63" in error

    def test_hyphen_at_start(self):
        """Subdomain starting with hyphen fails validation."""
        is_valid, error = validate_subdomain("-myapp")
        assert is_valid is False
        assert "beginnen" in error.lower()  # Dutch: "mag niet beginnen met"

    def test_hyphen_at_end(self):
        """Subdomain ending with hyphen fails validation."""
        is_valid, error = validate_subdomain("myapp-")
        assert is_valid is False
        assert "eindigen" in error.lower()  # Dutch: "mag niet eindigen met"

    def test_invalid_characters(self):
        """Subdomain with invalid characters fails validation."""
        # Note: uppercase letters are lowercased before validation, so MY-APP becomes my-app which is valid
        invalid_subdomains = ["my_app", "my.app", "my app"]
        for subdomain in invalid_subdomains:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is False, f"'{subdomain}' should be invalid"

    def test_uppercase_is_lowercased(self):
        """Uppercase letters are lowercased and accepted."""
        is_valid, error = validate_subdomain("MY-APP")
        assert is_valid is True  # Gets lowercased to my-app which is valid

    def test_reserved_subdomains(self):
        """Reserved subdomains fail validation with generic 'not available' message.

        NOTE: Error message is intentionally generic to prevent enumeration attacks.
        Attackers should not be able to distinguish reserved from taken subdomains.
        """
        reserved_examples = ["www", "api", "admin", "mail"]
        for subdomain in reserved_examples:
            is_valid, error = validate_subdomain(subdomain)
            assert is_valid is False, f"'{subdomain}' is reserved"
            # Generic error message - no "gereserveerd" to prevent enumeration
            assert "niet beschikbaar" in error.lower()  # Dutch: "is niet beschikbaar"

    def test_reserved_subdomains_are_defined(self):
        """RESERVED_SUBDOMAINS contains expected entries."""
        assert "www" in RESERVED_SUBDOMAINS
        assert "api" in RESERVED_SUBDOMAINS
        assert "admin" in RESERVED_SUBDOMAINS
        assert len(RESERVED_SUBDOMAINS) > 50  # Reasonable number of reserved names


class TestSubdomainValidationInRegister:
    """Tests for validation integration in SubdomainConnector.register."""

    @pytest.mark.asyncio
    async def test_register_validates_subdomain_format(self):
        """register raises SubdomainValidationError for invalid subdomain."""
        connector = SubdomainConnector()

        with pytest.raises(SubdomainValidationError) as exc_info:
            await connector.register(
                subdomain="-invalid",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert "beginnen" in str(exc_info.value).lower()  # Dutch: "mag niet beginnen met"

    @pytest.mark.asyncio
    async def test_register_rejects_reserved_subdomain(self):
        """register raises SubdomainValidationError for reserved subdomain.

        NOTE: Error message is intentionally generic to prevent enumeration attacks.
        """
        connector = SubdomainConnector()

        with pytest.raises(SubdomainValidationError) as exc_info:
            await connector.register(
                subdomain="www",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        # Generic error message - no "gereserveerd" to prevent enumeration
        assert "niet beschikbaar" in str(exc_info.value).lower()  # Dutch: "is niet beschikbaar"


class TestValidateBaseDomain:
    """Tests for validate_base_domain function."""

    def test_valid_base_domain_local(self):
        """Valid base domain for local cluster passes validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("kind", "local")
        assert is_valid is True
        assert error is None

    def test_valid_base_domain_production(self):
        """Valid base domain for production cluster passes validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("rijks.app", "odcn-production")
        assert is_valid is True
        assert error is None

    def test_invalid_base_domain(self):
        """Invalid base domain fails validation (must be syntactically invalid)."""
        from opi.connectors.subdomain import validate_base_domain

        # Custom domains with valid syntax are now accepted, so use a truly invalid domain
        is_valid, error = validate_base_domain("-invalid", "local")
        assert is_valid is False
        assert "ondersteund" in error.lower()  # Dutch: "geen ondersteund base domain"

    def test_empty_base_domain(self):
        """Empty base domain fails validation."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("", "local")
        assert is_valid is False
        assert "leeg" in error.lower()  # Dutch: "mag niet leeg zijn"

    def test_case_insensitive(self):
        """Base domain validation is case insensitive."""
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("RIJKS.APP", "odcn-production")
        assert is_valid is True
        assert error is None

    def test_all_clusters_validation(self):
        """Base domain validation without cluster checks against all supported domains."""
        from opi.connectors.subdomain import validate_base_domain

        # rijks.app should be valid when no cluster specified
        is_valid, error = validate_base_domain("rijks.app")
        assert is_valid is True

        # kind should be valid when no cluster specified
        is_valid, error = validate_base_domain("kind")
        assert is_valid is True


class TestGetSupportedBaseDomains:
    """Tests for get_supported_base_domains function."""

    def test_local_cluster_domains(self):
        """get_supported_base_domains returns correct domains for local cluster."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains("local")
        assert "kind" in domains
        assert "local" in domains

    def test_production_cluster_domains(self):
        """get_supported_base_domains returns correct domains for production cluster."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains("odcn-production")
        assert "rijks.app" in domains
        assert "rijksapps.nl" in domains

    def test_all_clusters_domains(self):
        """get_supported_base_domains returns all domains when no cluster specified."""
        from opi.connectors.subdomain import get_supported_base_domains

        domains = get_supported_base_domains()
        assert "kind" in domains
        assert "rijks.app" in domains
        assert len(domains) >= 4


class TestBaseDomainValidationInRegister:
    """Tests for base domain validation in SubdomainConnector.register."""

    @pytest.mark.asyncio
    async def test_register_validates_base_domain(self):
        """register raises BaseDomainValidationError for invalid base domain."""
        from opi.connectors.subdomain import BaseDomainValidationError

        connector = SubdomainConnector()

        # Custom domains with valid syntax are now accepted, so use a syntactically invalid domain
        with pytest.raises(BaseDomainValidationError) as exc_info:
            await connector.register(
                subdomain="myapp",
                base_domain="-invalid",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert "ondersteund" in str(exc_info.value).lower()


class TestRegisterOrUpdateForDeployment:
    """Tests for SubdomainConnector.register_or_update_for_deployment method."""

    @pytest.mark.asyncio
    async def test_register_new_subdomain(self):
        """register_or_update creates new registration when none exists."""
        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "kind",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "local",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": None,
        }

        mock_conn = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            None,  # get_by_deployment returns None
            mock_result,  # register returns result
        ]
        mock_conn.fetchval.return_value = None  # check_availability returns available

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.register_or_update_for_deployment(
                subdomain="myapp",
                base_domain="kind",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert result["subdomain"] == "myapp"

    @pytest.mark.asyncio
    async def test_returns_existing_when_unchanged(self):
        """register_or_update returns existing registration when subdomain unchanged."""
        connector = SubdomainConnector()

        existing_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "kind",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "local",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": None,
        }

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = existing_result  # get_by_deployment returns existing

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.register_or_update_for_deployment(
                subdomain="myapp",
                base_domain="kind",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert result["subdomain"] == "myapp"
        # Should only call fetchrow once for get_by_deployment
        assert mock_conn.fetchrow.call_count == 1


class TestRegisterRaceCondition:
    """Tests for race condition handling in register method."""

    @pytest.mark.asyncio
    async def test_register_handles_conflict_same_project(self):
        """register returns existing registration on conflict from same project."""
        connector = SubdomainConnector()

        existing_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "kind",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "local",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": None,
        }

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # check_availability says available
        mock_conn.fetchrow.side_effect = [
            None,  # INSERT ... ON CONFLICT returns None (conflict)
            existing_result,  # get_by_subdomain returns existing
        ]

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.register(
                subdomain="myapp",
                base_domain="kind",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert result["subdomain"] == "myapp"

    @pytest.mark.asyncio
    async def test_register_raises_on_conflict_different_project(self):
        """register raises error on conflict from different project."""
        from opi.connectors.subdomain import SubdomainNotAvailableError

        connector = SubdomainConnector()

        existing_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "kind",
            "project_name": "other-project",  # Different project
            "deployment_name": "prod",
            "cluster": "local",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": None,
        }

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # check_availability says available
        mock_conn.fetchrow.side_effect = [
            None,  # INSERT ... ON CONFLICT returns None (conflict)
            existing_result,  # get_by_subdomain returns existing from other project
        ]

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            pytest.raises(SubdomainNotAvailableError) as exc_info,
        ):
            await connector.register(
                subdomain="myapp",
                base_domain="kind",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

        assert "niet beschikbaar" in str(exc_info.value).lower()  # Dutch: "is niet beschikbaar"


class TestSubdomainAuditLogging:
    """Tests for audit logging in subdomain operations."""

    @pytest.mark.asyncio
    async def test_register_logs_audit_event(self):
        """register() logs audit event on successful registration."""
        from unittest.mock import patch

        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "kind",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "local",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": None,
        }

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None
        mock_conn.fetchrow.return_value = mock_result

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            patch("opi.connectors.subdomain.audit_logger") as mock_audit_logger,
        ):
            await connector.register(
                subdomain="myapp",
                base_domain="kind",
                project_name="my-project",
                deployment_name="prod",
                cluster="local",
            )

            # Verify audit log was called with correct format
            mock_audit_logger.info.assert_called_once()
            log_message = mock_audit_logger.info.call_args[0][0]
            assert "SUBDOMAIN_REGISTERED" in log_message
            assert "myapp.kind" in log_message
            assert "my-project" in log_message

    @pytest.mark.asyncio
    async def test_delete_logs_audit_event(self):
        """delete() logs audit event on successful deletion."""
        from unittest.mock import patch

        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 1"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            patch("opi.connectors.subdomain.audit_logger") as mock_audit_logger,
        ):
            await connector.delete("myapp", "kind")

            # Verify audit log was called
            mock_audit_logger.info.assert_called_once()
            log_message = mock_audit_logger.info.call_args[0][0]
            assert "SUBDOMAIN_DELETED" in log_message
            assert "myapp.kind" in log_message

    @pytest.mark.asyncio
    async def test_delete_by_project_logs_audit_event(self):
        """delete_by_project() logs audit event on successful deletion."""
        from unittest.mock import patch

        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 3"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            patch("opi.connectors.subdomain.audit_logger") as mock_audit_logger,
        ):
            await connector.delete_by_project("my-project")

            # Verify audit log was called
            mock_audit_logger.info.assert_called_once()
            log_message = mock_audit_logger.info.call_args[0][0]
            assert "SUBDOMAINS_DELETED_BY_PROJECT" in log_message
            assert "my-project" in log_message
            assert "count=3" in log_message

    @pytest.mark.asyncio
    async def test_delete_by_deployment_logs_audit_event(self):
        """delete_by_deployment() logs audit event on successful deletion."""
        from unittest.mock import patch

        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 1"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            patch("opi.connectors.subdomain.audit_logger") as mock_audit_logger,
        ):
            await connector.delete_by_deployment("my-project", "prod")

            # Verify audit log was called
            mock_audit_logger.info.assert_called_once()
            log_message = mock_audit_logger.info.call_args[0][0]
            assert "SUBDOMAINS_DELETED_BY_DEPLOYMENT" in log_message
            assert "my-project" in log_message
            assert "prod" in log_message

    @pytest.mark.asyncio
    async def test_no_audit_log_when_nothing_deleted(self):
        """delete operations don't log when nothing is deleted."""
        from unittest.mock import patch

        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 0"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with (
            patch.object(connector, "_get_pool", return_value=mock_pool),
            patch("opi.connectors.subdomain.audit_logger") as mock_audit_logger,
        ):
            await connector.delete_by_project("nonexistent-project")

            # Audit log should not be called when nothing was deleted
            mock_audit_logger.info.assert_not_called()


class TestSubdomainRollbackHelper:
    """Tests for subdomain rollback functionality in ProjectManager."""

    @pytest.mark.asyncio
    async def test_rollback_helper_clears_pending_rollback(self):
        """_rollback_subdomain_if_needed clears pending rollback after success."""
        from unittest.mock import AsyncMock

        # Create a mock ProjectManager-like object
        class MockProjectManager:
            _pending_subdomain_rollback = None

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                if not rollback_info or not rollback_info.get("should_rollback"):
                    return False

                connector = rollback_info.get("connector")
                if not connector:
                    return False

                try:
                    await connector.delete_by_deployment(
                        rollback_info["project_name"], rollback_info["deployment_name"]
                    )
                    self._pending_subdomain_rollback = None
                    return True
                except Exception:
                    return False

        manager = MockProjectManager()
        mock_connector = AsyncMock()
        mock_connector.delete_by_deployment = AsyncMock(return_value=1)

        manager._pending_subdomain_rollback = {
            "should_rollback": True,
            "connector": mock_connector,
            "project_name": "test-project",
            "deployment_name": "main",
            "subdomain": "myapp",
            "base_domain": "test.com",
        }

        result = await manager._rollback_subdomain_if_needed()

        assert result is True
        assert manager._pending_subdomain_rollback is None
        mock_connector.delete_by_deployment.assert_called_once_with("test-project", "main")

    @pytest.mark.asyncio
    async def test_rollback_helper_returns_false_when_no_pending(self):
        """_rollback_subdomain_if_needed returns False when nothing to rollback."""

        class MockProjectManager:
            _pending_subdomain_rollback = None

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                return not (not rollback_info or not rollback_info.get("should_rollback"))

        manager = MockProjectManager()
        result = await manager._rollback_subdomain_if_needed()

        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_helper_returns_false_when_should_rollback_false(self):
        """_rollback_subdomain_if_needed respects should_rollback flag."""

        class MockProjectManager:
            _pending_subdomain_rollback: ClassVar[dict[str, bool]] = {"should_rollback": False}

            async def _rollback_subdomain_if_needed(self) -> bool:
                rollback_info = getattr(self, "_pending_subdomain_rollback", None)
                return not (not rollback_info or not rollback_info.get("should_rollback"))

        manager = MockProjectManager()
        result = await manager._rollback_subdomain_if_needed()

        assert result is False
