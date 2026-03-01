"""Tests for resource extraction from project YAML data."""

from opi.handlers.project_file_handler import (
    DEFAULT_RESOURCES,
    ProjectFileHandler,
    _parse_resources_block,
    _parse_resources_block_partial,
)


class TestParseResourcesBlock:
    """Tests for the _parse_resources_block helper."""

    def test_none_returns_defaults(self):
        result = _parse_resources_block(None)
        assert result == DEFAULT_RESOURCES

    def test_empty_dict_returns_defaults(self):
        result = _parse_resources_block({})
        assert result == DEFAULT_RESOURCES

    def test_partial_override_requests_memory(self):
        raw = {"requests": {"memory": "256Mi"}}
        result = _parse_resources_block(raw)
        assert result["requests_memory"] == "256Mi"
        assert result["requests_cpu"] == DEFAULT_RESOURCES["requests_cpu"]
        assert result["limits_memory"] == DEFAULT_RESOURCES["limits_memory"]
        assert result["limits_cpu"] == DEFAULT_RESOURCES["limits_cpu"]

    def test_full_override(self):
        raw = {
            "requests": {"memory": "256Mi", "cpu": "100m"},
            "limits": {"memory": "1024Mi", "cpu": "2000m"},
        }
        result = _parse_resources_block(raw)
        assert result == {
            "requests_memory": "256Mi",
            "requests_cpu": "100m",
            "limits_memory": "1024Mi",
            "limits_cpu": "2000m",
        }

    def test_custom_defaults(self):
        custom = {
            "requests_memory": "64Mi",
            "requests_cpu": "25m",
            "limits_memory": "256Mi",
            "limits_cpu": "500m",
        }
        result = _parse_resources_block(None, defaults=custom)
        assert result == custom

    def test_non_dict_returns_defaults(self):
        result = _parse_resources_block("invalid")  # type: ignore[arg-type]
        assert result == DEFAULT_RESOURCES


class TestParseResourcesBlockPartial:
    """Tests for the _parse_resources_block_partial helper."""

    def test_none_returns_empty(self):
        result = _parse_resources_block_partial(None)
        assert result == {}

    def test_empty_dict_returns_empty(self):
        result = _parse_resources_block_partial({})
        assert result == {}

    def test_partial_limits_memory(self):
        raw = {"limits": {"memory": "2048Mi"}}
        result = _parse_resources_block_partial(raw)
        assert result == {"limits_memory": "2048Mi"}

    def test_full_override(self):
        raw = {
            "requests": {"memory": "256Mi", "cpu": "100m"},
            "limits": {"memory": "1024Mi", "cpu": "2000m"},
        }
        result = _parse_resources_block_partial(raw)
        assert result == {
            "requests_memory": "256Mi",
            "requests_cpu": "100m",
            "limits_memory": "1024Mi",
            "limits_cpu": "2000m",
        }


class TestExtractComponentResources:
    """Tests for ProjectFileHandler.extract_component_resources."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_no_resources_returns_defaults(self):
        project_data = {"components": [{"name": "api", "image": "nginx:latest"}]}
        result = self.handler.extract_component_resources(project_data, "api")
        assert result == DEFAULT_RESOURCES

    def test_partial_resources(self):
        project_data = {
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "256Mi"},
                    },
                }
            ]
        }
        result = self.handler.extract_component_resources(project_data, "api")
        assert result["requests_memory"] == "256Mi"
        assert result["limits_memory"] == DEFAULT_RESOURCES["limits_memory"]

    def test_full_resources(self):
        project_data = {
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "256Mi", "cpu": "100m"},
                        "limits": {"memory": "1Gi", "cpu": "2000m"},
                    },
                }
            ]
        }
        result = self.handler.extract_component_resources(project_data, "api")
        assert result == {
            "requests_memory": "256Mi",
            "requests_cpu": "100m",
            "limits_memory": "1Gi",
            "limits_cpu": "2000m",
        }

    def test_nonexistent_component(self):
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.extract_component_resources(project_data, "nonexistent")
        assert result == DEFAULT_RESOURCES


class TestExtractDeploymentComponentResources:
    """Tests for ProjectFileHandler.extract_deployment_component_resources."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_no_resources_returns_none(self):
        project_data = {
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "api"}],
                }
            ]
        }
        result = self.handler.extract_deployment_component_resources(project_data, "production", "api")
        assert result is None

    def test_partial_override(self):
        project_data = {
            "deployments": [
                {
                    "name": "production",
                    "components": [
                        {
                            "reference": "api",
                            "resources": {"limits": {"memory": "2048Mi"}},
                        }
                    ],
                }
            ]
        }
        result = self.handler.extract_deployment_component_resources(project_data, "production", "api")
        assert result == {"limits_memory": "2048Mi"}

    def test_nonexistent_deployment(self):
        project_data = {"deployments": [{"name": "staging", "components": []}]}
        result = self.handler.extract_deployment_component_resources(project_data, "production", "api")
        assert result is None


class TestMergeLogic:
    """Tests for the merge logic (deployment overrides component)."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_deployment_overrides_component(self):
        project_data = {
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "128Mi", "cpu": "50m"},
                        "limits": {"memory": "512Mi", "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "components": [
                        {
                            "reference": "api",
                            "resources": {"limits": {"memory": "2048Mi"}},
                        }
                    ],
                }
            ],
        }

        component_resources = self.handler.extract_component_resources(project_data, "api")
        deployment_resources = self.handler.extract_deployment_component_resources(project_data, "production", "api")

        assert deployment_resources is not None
        component_resources.update(deployment_resources)

        assert component_resources["requests_memory"] == "128Mi"
        assert component_resources["requests_cpu"] == "50m"
        assert component_resources["limits_memory"] == "2048Mi"
        assert component_resources["limits_cpu"] == "1000m"


class TestExtractComponentDisabled:
    """Tests for ProjectFileHandler.extract_component_disabled."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_not_disabled_by_default(self):
        project_data = {"components": [{"name": "api"}]}
        disabled, reason = self.handler.extract_component_disabled(project_data, "api")
        assert disabled is False
        assert reason == ""

    def test_disabled_with_reason(self):
        project_data = {
            "components": [
                {
                    "name": "api",
                    "disabled": True,
                    "disabled-reason": "CrashLoopBackOff detected",
                }
            ]
        }
        disabled, reason = self.handler.extract_component_disabled(project_data, "api")
        assert disabled is True
        assert reason == "CrashLoopBackOff detected"


class TestSetComponentDisabled:
    """Tests for ProjectFileHandler.set_component_disabled."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_set_disabled(self):
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.set_component_disabled(project_data, "api", True, "broken")
        assert result is True
        assert project_data["components"][0]["disabled"] is True
        assert project_data["components"][0]["disabled-reason"] == "broken"

    def test_set_enabled_removes_reason(self):
        project_data = {"components": [{"name": "api", "disabled": True, "disabled-reason": "broken"}]}
        result = self.handler.set_component_disabled(project_data, "api", False, "")
        assert result is True
        assert project_data["components"][0]["disabled"] is False
        assert "disabled-reason" not in project_data["components"][0]

    def test_nonexistent_component(self):
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.set_component_disabled(project_data, "nonexistent", True, "broken")
        assert result is False


class TestSetComponentResources:
    """Tests for ProjectFileHandler.set_component_resources."""

    def setup_method(self):
        self.handler = ProjectFileHandler()

    def test_set_resources_new(self):
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.set_component_resources(
            project_data, "api", {"limits_memory": "1024Mi", "requests_memory": "256Mi"}
        )
        assert result is True
        assert project_data["components"][0]["resources"]["limits"]["memory"] == "1024Mi"
        assert project_data["components"][0]["resources"]["requests"]["memory"] == "256Mi"

    def test_set_resources_update_existing(self):
        project_data = {
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "128Mi", "cpu": "50m"},
                        "limits": {"memory": "512Mi", "cpu": "1000m"},
                    },
                }
            ]
        }
        result = self.handler.set_component_resources(project_data, "api", {"limits_memory": "1024Mi"})
        assert result is True
        assert project_data["components"][0]["resources"]["limits"]["memory"] == "1024Mi"
        # cpu should be unchanged
        assert project_data["components"][0]["resources"]["limits"]["cpu"] == "1000m"

    def test_nonexistent_component(self):
        project_data = {"components": [{"name": "api"}]}
        result = self.handler.set_component_resources(project_data, "nonexistent", {"limits_memory": "1024Mi"})
        assert result is False
