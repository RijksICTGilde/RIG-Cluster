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

    def test_numeric_index_path(self):
        handler = ProjectFileHandler()
        result = handler._parse_deepdiff_path("root['deployments'][0]['name']")
        assert "deployments" in result
        assert "name" in result


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
