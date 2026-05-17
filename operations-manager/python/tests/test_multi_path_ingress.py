"""
Tests for multi-path ingress functionality.

Tests the ability to define multiple paths for a component, each generating
its own Kubernetes Ingress resource.
"""

from opi.handlers.project_file_handler import ProjectFileHandler
from opi.utils.naming import generate_ingress_name_from_path


class TestGenerateIngressNameFromPath:
    """Tests for generate_ingress_name_from_path function."""

    def test_root_path_returns_base_name(self):
        """Root path '/' should not add any suffix."""
        assert generate_ingress_name_from_path("main-api", "/") == "main-api"

    def test_empty_path_returns_base_name(self):
        """Empty path should not add any suffix."""
        assert generate_ingress_name_from_path("main-api", "") == "main-api"

    def test_simple_path_adds_suffix(self):
        """Simple path like '/api' should add '-api' suffix."""
        assert generate_ingress_name_from_path("main-api", "/api") == "main-api-api"

    def test_nested_path_flattens(self):
        """Nested paths like '/v1/users' should flatten to 'v1users'."""
        assert generate_ingress_name_from_path("main-api", "/v1/users") == "main-api-v1users"

    def test_path_with_hyphens_sanitizes(self):
        """Paths with hyphens should be sanitized."""
        assert generate_ingress_name_from_path("main-api", "/health-check") == "main-api-healthcheck"

    def test_path_with_underscores_sanitizes(self):
        """Paths with underscores should be sanitized."""
        assert generate_ingress_name_from_path("main-api", "/user_info") == "main-api-userinfo"

    def test_max_length_truncates(self):
        """Names exceeding max_length should be truncated."""
        long_path = "/very/long/path/that/exceeds/the/maximum/length"
        result = generate_ingress_name_from_path("deployment-component", long_path, max_length=30)
        assert len(result) <= 30

    def test_preserves_case_insensitivity(self):
        """Paths should be lowercase."""
        assert generate_ingress_name_from_path("main-api", "/API") == "main-api-api"
        assert generate_ingress_name_from_path("main-api", "/V1/Users") == "main-api-v1users"


class TestExtractComponentPaths:
    """Tests for ProjectFileHandler.extract_component_paths method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.handler = ProjectFileHandler()

    def test_simple_string_path(self):
        """Simple string path should return list with single item."""
        project_data = {"components": [{"name": "api", "path": "/api"}]}
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/api", "rewrite": None}]

    def test_default_path_when_missing(self):
        """Missing path should default to '/'."""
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/", "rewrite": None}]

    def test_list_of_match_objects(self):
        """List of match objects should be normalized."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": [
                        {"match": "/api"},
                        {"match": "/v1"},
                    ],
                }
            ]
        }
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [
            {"match": "/api", "rewrite": None},
            {"match": "/v1", "rewrite": None},
        ]

    def test_list_of_strings(self):
        """List of plain strings should be converted to match objects."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": ["/api", "/v1"],
                }
            ]
        }
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [
            {"match": "/api", "rewrite": None},
            {"match": "/v1", "rewrite": None},
        ]

    def test_rewrite_passes_through(self):
        """Rewrite option should be included in the path config."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": [
                        {"match": "/kader", "rewrite": "/"},
                    ],
                }
            ]
        }
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/kader", "rewrite": "/"}]

    def test_rewrite_none_when_not_specified(self):
        """Rewrite should be None when not specified in path config."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": [
                        {"match": "/api"},
                    ],
                }
            ]
        }
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/api", "rewrite": None}]

    def test_component_not_found(self):
        """Non-existent component should return default path."""
        project_data = {"components": [{"name": "other"}]}
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/", "rewrite": None}]

    def test_empty_components_list(self):
        """Empty components list should return default path."""
        project_data = {"components": []}
        result = self.handler.extract_component_paths(project_data, "api")
        assert result == [{"match": "/", "rewrite": None}]


class TestExtractDeploymentComponentPaths:
    """Tests for ProjectFileHandler.extract_deployment_component_paths method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.handler = ProjectFileHandler()

    def test_deployment_level_paths_take_precedence(self):
        """Deployment-level paths should override component-level path."""
        project_data = {"components": [{"name": "frontend", "path": "/"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {
                    "reference": "frontend",
                    "image": "nginx:latest",
                    "path": [
                        {"match": "/app", "rewrite": "/"},
                    ],
                }
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "frontend")
        assert result == [{"match": "/app", "rewrite": "/"}]

    def test_falls_back_to_component_level(self):
        """When no deployment-level paths, should fall back to component-level."""
        project_data = {"components": [{"name": "api", "path": "/api"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {"reference": "api", "image": "myapi:latest"},
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "api")
        assert result == [{"match": "/api", "rewrite": None}]

    def test_default_when_neither_present(self):
        """When neither deployment nor component paths exist, should return default."""
        project_data = {"components": [{"name": "app"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {"reference": "app", "image": "myapp:latest"},
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "app")
        assert result == [{"match": "/", "rewrite": None}]

    def test_multiple_deployment_paths(self):
        """Multiple deployment-level paths should all be returned."""
        project_data = {"components": [{"name": "frontend", "path": "/"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {
                    "reference": "frontend",
                    "image": "nginx:latest",
                    "path": [
                        {"match": "/app", "rewrite": "/"},
                        {"match": "/static"},
                    ],
                }
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "frontend")
        assert result == [
            {"match": "/app", "rewrite": "/"},
            {"match": "/static", "rewrite": None},
        ]

    def test_empty_deployment_paths_falls_back(self):
        """Empty deployment paths list should fall back to component-level."""
        project_data = {"components": [{"name": "api", "path": "/api"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {"reference": "api", "image": "myapi:latest", "path": []},
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "api")
        assert result == [{"match": "/api", "rewrite": None}]

    def test_component_not_in_deployment(self):
        """Component not in deployment should fall back to component-level."""
        project_data = {"components": [{"name": "api", "path": "/api"}]}
        deployment_data = {
            "name": "production",
            "components": [
                {"reference": "frontend", "image": "nginx:latest"},
            ],
        }
        result = self.handler.extract_deployment_component_paths(project_data, deployment_data, "api")
        assert result == [{"match": "/api", "rewrite": None}]


class TestExtractComponentPathBackwardCompatibility:
    """Tests for backward compatibility of extract_component_path method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.handler = ProjectFileHandler()

    def test_simple_string_path(self):
        """Simple string path should work as before."""
        project_data = {"components": [{"name": "api", "path": "/api"}]}
        result = self.handler.extract_component_path(project_data, "api")
        assert result == "/api"

    def test_list_match_returns_first(self):
        """List of match objects should return the first match for backward compatibility."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": [
                        {"match": "/api"},
                        {"match": "/v1"},
                    ],
                }
            ]
        }
        result = self.handler.extract_component_path(project_data, "api")
        assert result == "/api"

    def test_list_of_strings_returns_first(self):
        """List of string paths should return the first one."""
        project_data = {
            "components": [
                {
                    "name": "api",
                    "path": ["/api", "/v1"],
                }
            ]
        }
        result = self.handler.extract_component_path(project_data, "api")
        assert result == "/api"

    def test_default_path_when_missing(self):
        """Missing path should return default."""
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.extract_component_path(project_data, "api", default_path="/default")
        assert result == "/default"
