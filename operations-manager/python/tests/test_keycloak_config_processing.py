"""
Unit tests for Keycloak service configuration processing.

Tests verify that configuration extraction, validation, and template selection
work correctly without actually connecting to Keycloak.
"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from opi.manager.keycloak_manager import KeycloakManager
from opi.services import ServiceType


class TestKeycloakConfigExtraction:
    """Test configuration extraction from project data."""

    def setup_method(self):
        """Set up test fixtures."""
        # Mock ProjectManager
        self.mock_project_manager = Mock()
        self.keycloak_manager = KeycloakManager(self.mock_project_manager)

    def test_extract_config_with_default_template(self):
        """Test extracting config when no config specified (should use default)."""
        project_data = {
            "name": "test-project",
            "services": ["keycloak"],  # Simple string format, no config
        }

        config = self.keycloak_manager._get_keycloak_service_config(project_data)

        assert config["template"] == "sso-only"
        assert config["variables"] == {}

    def test_extract_config_with_custom_template(self):
        """Test extracting config with custom template specified."""
        project_data = {
            "name": "test-project",
            "services": [
                {
                    "keycloak": {
                        "config": {
                            "template": "algoritmeregister",
                            "variables": {
                                "frontend_redirect_uris": "https://example.com/*",
                                "realm_display_name": "Test Realm",
                            },
                        }
                    }
                }
            ],
        }

        config = self.keycloak_manager._get_keycloak_service_config(project_data)

        assert config["template"] == "algoritmeregister"
        assert config["variables"]["frontend_redirect_uris"] == "https://example.com/*"
        assert config["variables"]["realm_display_name"] == "Test Realm"

    def test_extract_config_with_template_only(self):
        """Test extracting config with template but no variables."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": "sso-support"}}}],
        }

        config = self.keycloak_manager._get_keycloak_service_config(project_data)

        assert config["template"] == "sso-support"
        assert config["variables"] == {}

    def test_extract_config_no_services(self):
        """Test extracting config when no services defined."""
        project_data = {"name": "test-project"}

        config = self.keycloak_manager._get_keycloak_service_config(project_data)

        assert config["template"] == "sso-only"
        assert config["variables"] == {}


class TestKeycloakConfigValidation:
    """Test configuration validation and error cases."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_project_manager = Mock()
        self.keycloak_manager = KeycloakManager(self.mock_project_manager)

    def test_invalid_template_file_raises_error(self):
        """Test that non-existent template file raises FileNotFoundError."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": "nonexistent-template"}}}],
        }

        with pytest.raises(FileNotFoundError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "nonexistent-template" in str(exc_info.value)
        assert "Available templates:" in str(exc_info.value)

    def test_path_traversal_attempt_raises_error(self):
        """Test that path traversal in template name is blocked."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": "../secrets/password"}}}],
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Invalid template name" in str(exc_info.value)
        assert "path separators" in str(exc_info.value)

    def test_forward_slash_in_template_raises_error(self):
        """Test that forward slash in template name is blocked."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": "configs/secret"}}}],
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Invalid template name" in str(exc_info.value)

    def test_backslash_in_template_raises_error(self):
        """Test that backslash in template name is blocked (Windows paths)."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": "configs\\secret"}}}],
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Invalid template name" in str(exc_info.value)

    def test_invalid_config_format_raises_error(self):
        """Test that non-dict config raises ValueError."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": "invalid-string-config"}}],
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Keycloak config must be a dict" in str(exc_info.value)

    def test_invalid_template_type_raises_error(self):
        """Test that non-string template raises ValueError."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": {"config": {"template": 123}}}],  # Number instead of string
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Template must be a string" in str(exc_info.value)

    def test_invalid_variables_type_raises_error(self):
        """Test that non-dict variables raises ValueError."""
        project_data = {
            "name": "test-project",
            "services": [
                {
                    "keycloak": {
                        "config": {
                            "template": "sso-only",
                            "variables": "invalid-string-variables",  # Should be dict
                        }
                    }
                }
            ],
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Template variables must be a dict" in str(exc_info.value)

    def test_invalid_service_format_raises_error(self):
        """Test that invalid service item format raises ValueError."""
        project_data = {
            "name": "test-project",
            "services": [{"keycloak": "invalid-not-dict"}],  # Should be dict with 'config' key
        }

        with pytest.raises(ValueError) as exc_info:
            self.keycloak_manager._get_keycloak_service_config(project_data)

        assert "Invalid keycloak service format" in str(exc_info.value)


class TestKeycloakRealmSetup:
    """Test realm setup with configuration."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_project_manager = AsyncMock()
        self.keycloak_manager = KeycloakManager(self.mock_project_manager)

    @pytest.mark.asyncio
    async def test_setup_realm_with_default_template(self):
        """Test realm setup uses correct template file."""
        config = {"template": "sso-only", "variables": {}}

        # Mock dependencies
        self.mock_project_manager.get_contents = AsyncMock(return_value={"config": {"age-public-key": "age1test..."}})

        with (
            patch("opi.manager.keycloak_manager.create_keycloak_connector") as mock_connector,
            patch("opi.manager.keycloak_manager.encrypt_age_content") as mock_encrypt,
            patch("opi.manager.keycloak_manager.KeycloakYamlHandler") as mock_handler_class,
        ):
            mock_keycloak = AsyncMock()
            mock_connector.return_value = mock_keycloak
            mock_encrypt.return_value = "encrypted-password"

            # Mock KeycloakYamlHandler
            mock_handler = AsyncMock()
            mock_handler_class.return_value = mock_handler

            # Mock Keycloak operations
            mock_keycloak.create_user = AsyncMock(return_value={"id": "user-123"})
            mock_keycloak.assign_realm_management_role = AsyncMock()

            # Call the method
            result = await self.keycloak_manager._setup_project_keycloak_realm(
                project_name="test-project",
                cluster="local",
                keycloak_url="http://keycloak.local",
                config=config,
            )

            # Verify KeycloakYamlHandler was called with correct template
            mock_handler.execute_config.assert_called_once()
            call_args = mock_handler.execute_config.call_args

            # Check template path
            template_path = call_args[0][0]  # First positional argument
            assert str(template_path).endswith("sso-only.yaml")
            assert "keycloak" in str(template_path)

            # Check context variables
            context = call_args[0][1]  # Second positional argument
            assert context["project_name"] == "test-project"
            assert context["cluster"] == "local"
            assert context["keycloak_url"] == "http://keycloak.local"
            assert "realm_name" in context
            assert "platform_realm_name" in context

    @pytest.mark.asyncio
    async def test_setup_realm_with_custom_template(self):
        """Test realm setup uses custom template when specified."""
        config = {
            "template": "algoritmeregister",
            "variables": {"frontend_redirect_uris": "https://test.com/*"},
        }

        # Mock dependencies
        self.mock_project_manager.get_contents = AsyncMock(return_value={"config": {"age-public-key": "age1test..."}})

        with (
            patch("opi.manager.keycloak_manager.create_keycloak_connector") as mock_connector,
            patch("opi.manager.keycloak_manager.encrypt_age_content") as mock_encrypt,
            patch("opi.manager.keycloak_manager.KeycloakYamlHandler") as mock_handler_class,
        ):
            mock_keycloak = AsyncMock()
            mock_connector.return_value = mock_keycloak
            mock_encrypt.return_value = "encrypted-password"

            # Mock KeycloakYamlHandler
            mock_handler = AsyncMock()
            mock_handler_class.return_value = mock_handler

            # Mock Keycloak operations
            mock_keycloak.create_user = AsyncMock(return_value={"id": "user-123"})
            mock_keycloak.assign_realm_management_role = AsyncMock()

            # Call the method
            result = await self.keycloak_manager._setup_project_keycloak_realm(
                project_name="test-project",
                cluster="local",
                keycloak_url="http://keycloak.local",
                config=config,
            )

            # Verify KeycloakYamlHandler was called with algoritmeregister template
            mock_handler.execute_config.assert_called_once()
            call_args = mock_handler.execute_config.call_args

            # Check template path
            template_path = call_args[0][0]
            assert str(template_path).endswith("algoritmeregister.yaml")

            # Check custom variables were merged into context
            context = call_args[0][1]
            assert context["frontend_redirect_uris"] == "https://test.com/*"

    @pytest.mark.asyncio
    async def test_setup_realm_missing_template_raises_error(self):
        """Test that missing template file raises FileNotFoundError."""
        config = {"template": "nonexistent", "variables": {}}

        # Mock dependencies
        self.mock_project_manager.get_contents = AsyncMock(return_value={"config": {"age-public-key": "age1test..."}})

        with (
            patch("opi.manager.keycloak_manager.create_keycloak_connector") as mock_connector,
            patch("opi.manager.keycloak_manager.encrypt_age_content") as mock_encrypt,
        ):
            mock_keycloak = AsyncMock()
            mock_connector.return_value = mock_keycloak
            mock_encrypt.return_value = "encrypted-password"

            # Call should raise FileNotFoundError
            with pytest.raises(FileNotFoundError) as exc_info:
                await self.keycloak_manager._setup_project_keycloak_realm(
                    project_name="test-project",
                    cluster="local",
                    keycloak_url="http://keycloak.local",
                    config=config,
                )

            assert "nonexistent.yaml" in str(exc_info.value)


class TestOldServiceNameHandling:
    """Test that old service name 'sso-rijk' is properly rejected."""

    def test_old_service_name_in_services_list(self):
        """Test that 'sso-rijk' in services list raises helpful error."""
        from opi.services import ServiceAdapter

        with pytest.raises(ValueError) as exc_info:
            ServiceAdapter.parse_services_from_strings(["sso-rijk"])

        assert "sso-rijk" in str(exc_info.value)
        assert "keycloak" in str(exc_info.value)
        assert "renamed" in str(exc_info.value).lower()

    def test_new_service_name_works(self):
        """Test that 'keycloak' service name works correctly."""
        from opi.services import ServiceAdapter

        services = ServiceAdapter.parse_services_from_strings(["keycloak"])

        assert len(services) == 1
        assert services[0] == ServiceType.KEYCLOAK


class TestTemplateFilesExist:
    """Test that expected template files exist."""

    def test_default_template_exists(self):
        """Test that default sso-only template exists."""
        template_path = Path(__file__).parent.parent / "opi" / "configs" / "keycloak" / "sso-only.yaml"
        assert template_path.exists(), f"Default template not found: {template_path}"

    def test_algoritmeregister_template_exists(self):
        """Test that algoritmeregister template exists."""
        template_path = Path(__file__).parent.parent / "opi" / "configs" / "keycloak" / "algoritmeregister.yaml"
        assert template_path.exists(), f"Algoritmeregister template not found: {template_path}"

    def test_bootstrap_template_exists(self):
        """Test that bootstrap template exists."""
        template_path = Path(__file__).parent.parent / "opi" / "configs" / "keycloak" / "bootstrap.yaml"
        assert template_path.exists(), f"Bootstrap template not found: {template_path}"


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
