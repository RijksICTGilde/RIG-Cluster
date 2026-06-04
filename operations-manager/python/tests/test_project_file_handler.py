"""
Tests for opi.handlers.project_file_handler module.

Tests for _parse_deepdiff_path, set_deployment_service_generation, and related methods.
"""

from opi.handlers.project_file_handler import ProjectFileHandler


class TestParseDeepDiffPath:
    """Tests for _parse_deepdiff_path."""

    def test_simple_path(self):
        handler = ProjectFileHandler()
        assert handler._parse_deepdiff_path("root['deployments']") == "deployments"

    def test_nested_path(self):
        handler = ProjectFileHandler()
        assert handler._parse_deepdiff_path("root['deployments']['web-app']") == "deployments.web-app"

    def test_path_with_root_in_key_name(self):
        """Bug: path.replace('root', '') removes 'root' from key names too."""
        handler = ProjectFileHandler()
        # Key name "rootCause" should be preserved
        result = handler._parse_deepdiff_path("root['rootCause']")
        assert result == "rootCause", f"Key 'rootCause' was corrupted: got '{result}'"

    def test_path_with_root_password_key(self):
        """Bug: 'root-password' becomes '-password' due to greedy replace."""
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['config']['root-password']")
        assert result == "config.root-password", f"Key 'root-password' was corrupted: got '{result}'"

    def test_path_with_root_in_middle_key(self):
        """Key containing 'root' substring should be preserved."""
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['settings']['rootDir']")
        assert result == "settings.rootDir", f"Key 'rootDir' was corrupted: got '{result}'"

    def test_numeric_index_converts_to_dot(self):
        """Bare numeric indices like [1] should convert to .1 for startswith('deployments.') to work."""
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['deployments'][1]")
        assert result == "deployments.1", f"Expected 'deployments.1', got '{result}'"

    def test_nested_numeric_indices(self):
        """Multiple numeric indices should all convert to dot notation."""
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['deployments'][0]['components'][1]")
        assert result == "deployments.0.components.1", f"Expected 'deployments.0.components.1', got '{result}'"

    def test_mixed_path_numeric_and_key(self):
        """Mix of numeric indices and string keys should all convert properly."""
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['deployments'][0]['name']")
        assert result == "deployments.0.name", f"Expected 'deployments.0.name', got '{result}'"


class TestSetDeploymentServiceGeneration:
    """Tests for set_deployment_service_generation."""

    def test_sets_generation_on_empty_services(self):
        handler = ProjectFileHandler()
        project_data = {"deployments": [{"name": "staging", "services": []}]}
        handler.set_deployment_service_generation(project_data, "staging", "database", 1)

        services = project_data["deployments"][0]["services"]
        assert len(services) == 1
        assert services[0]["reference"] == "database"
        assert services[0]["config"]["generation"] == 1

    def test_updates_existing_service_entry(self):
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "services": [{"reference": "database", "config": {"generation": 1}}],
                }
            ]
        }
        handler.set_deployment_service_generation(project_data, "staging", "database", 2)

        services = project_data["deployments"][0]["services"]
        assert len(services) == 1
        assert services[0]["config"]["generation"] == 2

    def test_dict_to_list_migration_preserves_existing_entries(self):
        """Bug: dict-to-list migration drops all existing entries."""
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "services": {
                        "database": {"generation": 1},
                        "minio-storage": {"generation": 2},
                    },
                }
            ]
        }
        # Set a new generation for database - this should also preserve minio-storage
        handler.set_deployment_service_generation(project_data, "staging", "database", 3)

        services = project_data["deployments"][0]["services"]
        assert isinstance(services, list), "Services should be converted to list"

        # Both services should be present
        refs = [s.get("reference") for s in services]
        assert "database" in refs, f"database entry should be preserved, got refs: {refs}"
        assert "minio-storage" in refs, f"minio-storage entry should be preserved, got refs: {refs}"

        # Database should have new generation
        db_entry = next(s for s in services if s["reference"] == "database")
        assert db_entry["config"]["generation"] == 3

    def test_creates_services_list_when_missing(self):
        handler = ProjectFileHandler()
        project_data = {"deployments": [{"name": "staging"}]}
        handler.set_deployment_service_generation(project_data, "staging", "database", 1)

        services = project_data["deployments"][0]["services"]
        assert isinstance(services, list)
        assert len(services) == 1


class TestDecryptAndCleanEnvVars:
    """Tests for _decrypt_and_clean_env_vars.

    Regression: this step used to strip surrounding quotes from every value,
    which silently corrupted env vars whose value must literally contain
    quotes (e.g. cal.com's ALLOWED_HOSTNAMES, which needs '"host"' so the app
    can wrap it into a JSON array). Quote semantics belong to the parser
    (validate_and_parse_env_vars), not to this decrypt step.
    """

    def test_preserves_intentional_surrounding_double_quotes(self):
        handler = ProjectFileHandler()
        result = handler._decrypt_and_clean_env_vars(
            {"ALLOWED_HOSTNAMES": '"productie-cp-byw.sandbox.rijksapp.dev"'}, None
        )
        assert result["ALLOWED_HOSTNAMES"] == '"productie-cp-byw.sandbox.rijksapp.dev"'

    def test_leaves_bare_values_untouched(self):
        handler = ProjectFileHandler()
        result = handler._decrypt_and_clean_env_vars({"PLAIN": "bare-value", "EMPTY": ""}, None)
        assert result == {"PLAIN": "bare-value", "EMPTY": ""}

    def test_preserves_literal_double_quote_pair(self):
        handler = ProjectFileHandler()
        result = handler._decrypt_and_clean_env_vars({"JSON_ARRAY": '"[]"'}, None)
        assert result["JSON_ARRAY"] == '"[]"'


class TestExtractComponentSecurity:
    """Tests for the hidden per-component ``security`` block extractor."""

    def test_returns_none_when_component_missing(self) -> None:
        handler = ProjectFileHandler()
        assert handler.extract_component_security({"components": []}, "missing") is None

    def test_returns_none_when_no_security_block(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web"}]}
        assert handler.extract_component_security(project_data, "web") is None

    def test_returns_full_block(self) -> None:
        handler = ProjectFileHandler()
        project_data = {
            "components": [
                {
                    "name": "web",
                    "security": {"run-as-user": 999, "run-as-group": 999, "fs-group": 999},
                }
            ]
        }
        result = handler.extract_component_security(project_data, "web")
        assert result == {"run-as-user": 999, "run-as-group": 999, "fs-group": 999}

    def test_returns_partial_block(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "security": {"run-as-user": 1002}}]}
        result = handler.extract_component_security(project_data, "web")
        assert result == {"run-as-user": 1002}

    def test_silently_drops_non_int_values(self) -> None:
        """Schema layer catches wrong types; extractor is defence in depth."""
        handler = ProjectFileHandler()
        project_data = {
            "components": [
                {
                    "name": "web",
                    "security": {"run-as-user": "bogus", "fs-group": 999},
                }
            ]
        }
        result = handler.extract_component_security(project_data, "web")
        assert result == {"fs-group": 999}

    def test_returns_none_when_security_is_not_a_dict(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "security": "nonsense"}]}
        assert handler.extract_component_security(project_data, "web") is None

    def test_booleans_are_not_treated_as_int(self) -> None:
        """In Python ``True == 1`` and ``isinstance(True, int)`` is True; explicitly excluded."""
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "security": {"run-as-user": True}}]}
        assert handler.extract_component_security(project_data, "web") is None
