"""Tests for opi.services.project_utils module."""

import logging

import pytest
from opi.services.project_utils import (
    ComponentValidationError,
    apply_literal_scalars,
    normalize_container_image,
    parse_aliases,
    validate_root_component,
)
from opi.utils.yaml_util import dump_yaml_for_go
from ruamel.yaml.scalarstring import LiteralScalarString


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
        with caplog.at_level(logging.WARNING, logger="opi.services.project_utils"):
            normalize_container_image("ghcr.io/MyOrg/MyRepo:latest")
        assert "uppercase" in caplog.text
        assert "ghcr.io/MyOrg/MyRepo:latest" in caplog.text
        assert "ghcr.io/myorg/myrepo:latest" in caplog.text

    def test_no_warning_when_already_lowercase(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opi.services.project_utils"):
            normalize_container_image("ghcr.io/myorg/myrepo:latest")
        assert caplog.text == ""


class TestValidateRootComponent:
    """Root component constraint validation."""

    def test_valid_root_on_dot_format_passes(self):
        validate_root_component("frontend", ["frontend", "worker"], "component.subdomain")

    def test_no_root_passes(self):
        validate_root_component(None, ["frontend", "worker"], "component.subdomain")

    def test_root_not_in_deployment_raises(self):
        with pytest.raises(ComponentValidationError, match="not a component in this deployment"):
            validate_root_component("missing", ["frontend", "backend"], "component.subdomain")

    def test_root_on_unsupported_format_is_inert(self):
        # Dash formats don't expose a root host; root-component is ignored, not an
        # error (e.g. a clone that inherited it from a dotted-format source).
        validate_root_component("frontend", ["frontend"], None)
        validate_root_component("docs", ["editor"], "component-deployment-project")

    def test_root_on_dot_format_validates_component(self):
        # A format that supports root-component still validates the named component.
        validate_root_component("frontend", ["frontend", "worker"], "component.deployment.project")
        with pytest.raises(ComponentValidationError, match="not a component in this deployment"):
            validate_root_component("missing", ["frontend"], "component.deployment.project")

    def test_none_root_always_passes(self):
        validate_root_component(None, [], "component.subdomain")
        validate_root_component(None, [], None)


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

    def test_a_numeric_scalar_keeps_the_form_it_had(self):
        # build_component_config writes this mapping back without a public key (0755 -> 755 otherwise).
        source = "RETRY_CODE: 0755\n"
        assert dump_yaml_for_go(parse_aliases(source)) == source


class TestApplyLiteralScalars:
    """AGE-encrypted values carry newlines, and the YAML dumper only keeps those readable
    as a ``|`` block scalar when the value is wrapped in ``LiteralScalarString``."""

    MULTILINE = "age1...\n-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----\n"

    def test_config_keys_with_newlines_become_literal_scalars(self):
        data = {"config": {"age-private-key": self.MULTILINE, "api-key": self.MULTILINE}}
        apply_literal_scalars(data)
        assert isinstance(data["config"]["age-private-key"], LiteralScalarString)
        assert isinstance(data["config"]["api-key"], LiteralScalarString)

    def test_a_single_line_value_is_left_as_a_plain_str(self):
        data = {"config": {"api-key": "no-newline-here"}}
        apply_literal_scalars(data)
        assert type(data["config"]["api-key"]) is str

    def test_repository_password_with_newlines_becomes_a_literal_scalar(self):
        data = {"repositories": [{"name": "main", "password": self.MULTILINE}]}
        apply_literal_scalars(data)
        assert isinstance(data["repositories"][0]["password"], LiteralScalarString)

    def test_component_user_env_vars_and_aliases_become_literal_scalars(self):
        data = {"components": [{"name": "web", "user-env-vars": self.MULTILINE, "aliases": self.MULTILINE}]}
        apply_literal_scalars(data)
        assert isinstance(data["components"][0]["user-env-vars"], LiteralScalarString)
        assert isinstance(data["components"][0]["aliases"], LiteralScalarString)

    def test_deployment_component_user_env_vars_becomes_a_literal_scalar(self):
        data = {
            "deployments": [{"name": "main", "components": [{"reference": "web", "user-env-vars": self.MULTILINE}]}]
        }
        apply_literal_scalars(data)
        assert isinstance(data["deployments"][0]["components"][0]["user-env-vars"], LiteralScalarString)

    def test_an_empty_project_is_left_untouched(self):
        data: dict = {}
        apply_literal_scalars(data)
        assert data == {}

    def test_a_non_dict_component_or_repository_entry_is_skipped(self):
        """A caller handing in a project still mid-normalization may have a tombstone or
        a bare string in these lists; the converter must not raise on it."""
        data = {
            "repositories": ["not-a-dict"],
            "components": [None],
            "deployments": [{"components": ["not-a-dict"]}],
        }
        apply_literal_scalars(data)
        assert data["repositories"] == ["not-a-dict"]
