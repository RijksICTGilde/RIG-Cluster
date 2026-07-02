"""
Tests for opi.handlers.project_file_handler module.

Tests for _parse_deepdiff_path, set_deployment_service_generation, and related methods.
"""

from opi.handlers.project_file_handler import (
    ProjectFileHandler,
    is_image_pull_disable_reason,
    is_mutable_image_tag,
)


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


class TestExtractComponentProbe:
    """Tests for the per-component ``probe`` block extractor."""

    def test_defaults_to_tcp_when_component_missing(self) -> None:
        handler = ProjectFileHandler()
        result = handler.extract_component_probe({"components": []}, "missing")
        assert result == {"scheme": "tcp", "readiness_path": "/", "liveness_path": "/"}

    def test_defaults_to_tcp_when_no_probe_block(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web"}]}
        result = handler.extract_component_probe(project_data, "web")
        assert result == {"scheme": "tcp", "readiness_path": "/", "liveness_path": "/"}

    def test_https_with_explicit_paths(self) -> None:
        handler = ProjectFileHandler()
        project_data = {
            "components": [
                {
                    "name": "web",
                    "probe": {"scheme": "https", "readiness-path": "/readyz", "liveness-path": "/healthz"},
                }
            ]
        }
        result = handler.extract_component_probe(project_data, "web")
        assert result == {"scheme": "https", "readiness_path": "/readyz", "liveness_path": "/healthz"}

    def test_http_scheme_defaults_paths_to_root(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "probe": {"scheme": "http"}}]}
        result = handler.extract_component_probe(project_data, "web")
        assert result == {"scheme": "http", "readiness_path": "/", "liveness_path": "/"}


class TestExtractComponentCommand:
    """Tests for the hidden per-component ``command`` extractors (component + deployment levels)."""

    def test_component_returns_none_when_component_missing(self) -> None:
        handler = ProjectFileHandler()
        assert handler.extract_component_command({"components": []}, "missing") is None

    def test_component_returns_none_when_no_command(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web"}]}
        assert handler.extract_component_command(project_data, "web") is None

    def test_component_returns_command_list(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "command": ["sh", "-c", "exec /app/bin/web"]}]}
        result = handler.extract_component_command(project_data, "web")
        assert result == ["sh", "-c", "exec /app/bin/web"]

    def test_component_returns_none_for_empty_list(self) -> None:
        """Defence in depth: schema rejects empty list earlier, but if it slips
        through we treat ``[]`` as "no override" rather than erasing ENTRYPOINT."""
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "command": []}]}
        assert handler.extract_component_command(project_data, "web") is None

    def test_component_returns_none_for_non_list(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "command": "sh -c exec"}]}
        assert handler.extract_component_command(project_data, "web") is None

    def test_component_returns_none_for_mixed_item_types(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"components": [{"name": "web", "command": ["sh", 42]}]}
        assert handler.extract_component_command(project_data, "web") is None

    def test_deployment_returns_none_when_no_override(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"deployments": [{"name": "prd", "components": [{"reference": "web"}]}]}
        assert handler.extract_deployment_component_command(project_data, "prd", "web") is None

    def test_deployment_returns_override(self) -> None:
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [
                {
                    "name": "prd",
                    "components": [{"reference": "web", "command": ["sh", "-c", "echo prd && exec /app"]}],
                }
            ]
        }
        result = handler.extract_deployment_component_command(project_data, "prd", "web")
        assert result == ["sh", "-c", "echo prd && exec /app"]

    def test_deployment_returns_none_when_deployment_missing(self) -> None:
        handler = ProjectFileHandler()
        project_data = {"deployments": []}
        assert handler.extract_deployment_component_command(project_data, "prd", "web") is None


class TestImagePullDisableReenable:
    """Auto-disable records the offending image; a changed image auto-re-enables."""

    def _project(self, image: str = "reg/app:v1") -> dict:
        return {
            "deployments": [
                {
                    "name": "prd",
                    "components": [{"reference": "web", "image": image}],
                }
            ]
        }

    def _comp(self, project_data: dict) -> dict:
        return project_data["deployments"][0]["components"][0]

    def test_is_image_pull_disable_reason(self) -> None:
        assert is_image_pull_disable_reason("ErrImagePull: manifest unknown (404)")
        assert is_image_pull_disable_reason("ImagePullBackOff: back-off pulling image")
        assert is_image_pull_disable_reason("InvalidImageName: bad ref")
        assert not is_image_pull_disable_reason("OOMKilled detected")
        assert not is_image_pull_disable_reason("5 restarts (threshold: 3)")

    def test_disable_records_offending_image_for_image_pull(self) -> None:
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:broken")
        handler.set_deployment_component_disabled(
            project_data, "prd", "web", True, "ErrImagePull: manifest unknown (404)"
        )
        comp = self._comp(project_data)
        assert comp["disabled"] is True
        assert comp["disabled-image"] == "reg/app:broken"

    def test_disable_does_not_record_image_for_non_image_reason(self) -> None:
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:v1")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "OOMKilled detected")
        comp = self._comp(project_data)
        assert comp["disabled"] is True
        assert "disabled-image" not in comp

    def test_enable_clears_reason_and_image(self) -> None:
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:broken")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")
        handler.set_deployment_component_disabled(project_data, "prd", "web", False, "")
        comp = self._comp(project_data)
        assert comp["disabled"] is False
        assert "disabled-reason" not in comp
        assert "disabled-image" not in comp

    def test_reenable_when_image_changed(self) -> None:
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:broken")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")
        # User points the component at a new image.
        self._comp(project_data)["image"] = "reg/app:fixed"

        reenabled = handler.reenable_components_with_changed_image(project_data)

        assert reenabled == [("prd", "web")]
        comp = self._comp(project_data)
        assert comp["disabled"] is False
        assert "disabled-reason" not in comp
        assert "disabled-image" not in comp

    def test_no_reenable_when_image_unchanged(self) -> None:
        """The automated re-process after a disable does not change the image -> no flapping."""
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:broken")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")

        reenabled = handler.reenable_components_with_changed_image(project_data)

        assert reenabled == []
        assert self._comp(project_data)["disabled"] is True

    def test_no_reenable_for_non_image_disable(self) -> None:
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:v1")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "OOMKilled detected")
        # Even if the image changes, an OOM disable is not an image-pull disable.
        self._comp(project_data)["image"] = "reg/app:v2"

        reenabled = handler.reenable_components_with_changed_image(project_data)

        assert reenabled == []
        assert self._comp(project_data)["disabled"] is True

    def test_is_mutable_image_tag(self) -> None:
        assert is_mutable_image_tag("reg/app:latest")
        assert is_mutable_image_tag("reg/app")  # no tag -> :latest
        assert is_mutable_image_tag("reg/app:main")
        assert is_mutable_image_tag("rcr.rijksapps.nl:5000/app:latest")  # host port not a tag
        assert not is_mutable_image_tag("reg/app:v1.2.3")
        assert not is_mutable_image_tag("reg/app:a90bff069766")
        assert not is_mutable_image_tag("reg/app@sha256:deadbeef")  # digest-pinned
        assert not is_mutable_image_tag("")

    def test_no_mutable_retry_when_not_allowed(self) -> None:
        """Automated refresh (allow_mutable_retry=False) must not retry a :latest disable."""
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:latest")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")

        reenabled = handler.reenable_components_with_changed_image(project_data, allow_mutable_retry=False)

        assert reenabled == []
        assert self._comp(project_data)["disabled"] is True

    def test_mutable_retry_reenables_latest_on_redeploy(self) -> None:
        """Explicit user redeploy (allow_mutable_retry=True) retries a :latest disable."""
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:latest")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")

        reenabled = handler.reenable_components_with_changed_image(project_data, allow_mutable_retry=True)

        assert reenabled == [("prd", "web")]
        comp = self._comp(project_data)
        assert comp["disabled"] is False
        assert "disabled-image" not in comp

    def test_mutable_retry_does_not_touch_versioned_tag(self) -> None:
        """Even on redeploy, an unchanged versioned tag is not retried (only string-change)."""
        handler = ProjectFileHandler()
        project_data = self._project("reg/app:v1.2.3")
        handler.set_deployment_component_disabled(project_data, "prd", "web", True, "ErrImagePull: x")

        reenabled = handler.reenable_components_with_changed_image(project_data, allow_mutable_retry=True)

        assert reenabled == []
        assert self._comp(project_data)["disabled"] is True

    def test_reenable_respects_deployment_filter(self) -> None:
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [
                {"name": "prd", "components": [{"reference": "web", "image": "reg/app:broken"}]},
                {"name": "acc", "components": [{"reference": "web", "image": "reg/app:broken"}]},
            ]
        }
        for dep in ("prd", "acc"):
            handler.set_deployment_component_disabled(project_data, dep, "web", True, "ErrImagePull: x")
        for dep in project_data["deployments"]:
            dep["components"][0]["image"] = "reg/app:fixed"

        reenabled = handler.reenable_components_with_changed_image(project_data, deployment_names=["prd"])

        assert reenabled == [("prd", "web")]
        assert project_data["deployments"][0]["components"][0]["disabled"] is False
        assert project_data["deployments"][1]["components"][0]["disabled"] is True
