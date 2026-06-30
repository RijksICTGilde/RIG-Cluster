"""Tests for opi.utils.project_utils module."""

import logging

import pytest
from opi.utils.project_utils import (
    ComponentValidationError,
    normalize_container_image,
    parse_aliases,
    validate_component_paths,
    validate_root_component,
)


class TestNormalizeContainerImage:
    """OCI spec requires lowercase repository names - normalization must catch violations."""

    def test_already_lowercase_is_noop(self):
        image, was_normalized = normalize_container_image("ghcr.io/org/repo:latest")
        assert image == "ghcr.io/org/repo:latest"
        assert was_normalized is False

    def test_mixed_case_is_lowercased(self):
        image, was_normalized = normalize_container_image(
            "ghcr.io/BureauArchitectuurDigitaleOverheid/bouwmeester/backend:main"
        )
        assert image == "ghcr.io/bureauarchitectuurdigitaleoverheid/bouwmeester/backend:main"
        assert was_normalized is True

    def test_uppercase_tag_is_also_lowercased(self):
        """Tags are part of the image reference and get lowercased too."""
        image, was_normalized = normalize_container_image("nginx:Latest")
        assert image == "nginx:latest"
        assert was_normalized is True

    def test_fully_uppercase(self):
        image, was_normalized = normalize_container_image("GHCR.IO/ORG/REPO:V1")
        assert image == "ghcr.io/org/repo:v1"
        assert was_normalized is True

    def test_digest_reference(self):
        ref = "ghcr.io/Org/Repo@sha256:abc123"
        image, was_normalized = normalize_container_image(ref)
        assert image == "ghcr.io/org/repo@sha256:abc123"
        assert was_normalized is True

    def test_simple_image_no_registry(self):
        image, was_normalized = normalize_container_image("nginx:1.21")
        assert image == "nginx:1.21"
        assert was_normalized is False

    def test_logs_warning_on_normalization(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opi.utils.project_utils"):
            normalize_container_image("ghcr.io/MyOrg/MyRepo:latest")
        assert "uppercase" in caplog.text
        assert "ghcr.io/MyOrg/MyRepo:latest" in caplog.text
        assert "ghcr.io/myorg/myrepo:latest" in caplog.text

    def test_no_warning_when_already_lowercase(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opi.utils.project_utils"):
            normalize_container_image("ghcr.io/myorg/myrepo:latest")
        assert caplog.text == ""


class TestValidateComponentPaths:
    """Path uniqueness validation for shared-domain modes."""

    def test_unique_paths_shared_domain_passes(self):
        validate_component_paths(["/", "/api", "/admin"], "deployment-name")

    def test_duplicate_paths_shared_domain_raises(self):
        with pytest.raises(ComponentValidationError, match="Duplicate paths found"):
            validate_component_paths(["/", "/api", "/"], "deployment-name")

    def test_duplicate_paths_custom_mode_raises(self):
        with pytest.raises(ComponentValidationError, match="Duplicate paths found"):
            validate_component_paths(["/api", "/api"], "custom")

    def test_duplicate_paths_component_specific_mode_allowed(self):
        validate_component_paths(["/", "/"], "component-specific")

    def test_duplicate_paths_nice_url_mode_allowed(self):
        validate_component_paths(["/", "/"], "nice-url")

    def test_empty_components_passes(self):
        validate_component_paths([], "deployment-name")


class TestValidateRootComponent:
    """Root component constraint validation."""

    def test_valid_root_in_nice_url_passes(self):
        validate_root_component("frontend", ["frontend", "worker"], "nice-url")

    def test_no_root_passes(self):
        validate_root_component(None, ["frontend", "worker"], "nice-url")

    def test_root_not_in_deployment_raises(self):
        with pytest.raises(ComponentValidationError, match="not a component in this deployment"):
            validate_root_component("missing", ["frontend", "backend"], "nice-url")

    def test_root_on_unsupported_format_is_inert(self):
        # Dash formats don't expose a root host; root-component is ignored, not an
        # error (e.g. a clone that inherited it from a nice-url source).
        validate_root_component("frontend", ["frontend"], "component-specific")
        validate_root_component("docs", ["editor"], "component-specific", "component-deployment-project")

    def test_root_on_dot_format_validates_component(self):
        # A format that supports root-component still validates the named component.
        validate_root_component(
            "frontend", ["frontend", "worker"], "component-specific", "component.deployment.project"
        )
        with pytest.raises(ComponentValidationError, match="not a component in this deployment"):
            validate_root_component("missing", ["frontend"], "component-specific", "component.deployment.project")

    def test_none_root_always_passes(self):
        validate_root_component(None, [], "nice-url")
        validate_root_component(None, [], "deployment-name")


class TestParseAliases:
    """YAML aliases parsing."""

    def test_valid_yaml_parsed_to_dict(self):
        result = parse_aliases("DATABASE_URL: $HOST:$PORT/$DB_NAME\nS3: $OBJECT_STORE_URL")
        assert result == {"DATABASE_URL": "$HOST:$PORT/$DB_NAME", "S3": "$OBJECT_STORE_URL"}

    def test_invalid_yaml_raises(self):
        with pytest.raises(ComponentValidationError, match="Invalid aliases format"):
            parse_aliases("key: [unclosed bracket")

    def test_empty_yaml_returns_empty_dict(self):
        result = parse_aliases("")
        assert result == {}

    def test_non_dict_yaml_returns_empty_dict(self):
        result = parse_aliases("just a string")
        assert result == {}

    def test_single_alias(self):
        result = parse_aliases("DB_URL: $DATABASE_SERVER_HOST")
        assert result == {"DB_URL": "$DATABASE_SERVER_HOST"}
