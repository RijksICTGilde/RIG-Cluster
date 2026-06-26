"""Tests for re-enabling components disabled by ImagePullBackOff on image update."""

from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.oom_watcher import ComponentFailure


class TestImagePullDisabledReset:
    """Test that components disabled due to ImagePullBackOff are re-enabled on image change."""

    def _project_data(self, disabled: bool = False, reason: str = "") -> dict:
        data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [
                        {"reference": "api", "image": "ghcr.io/org/app:v1"},
                    ],
                }
            ],
        }
        if disabled:
            data["deployments"][0]["components"][0]["disabled"] = True
            data["deployments"][0]["components"][0]["disabled-reason"] = reason
        return data

    def test_extract_disabled_image_pull(self) -> None:
        handler = ProjectFileHandler()
        data = self._project_data(disabled=True, reason="ImagePullBackOff: image not found")

        is_disabled, reason = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled is True
        assert "ImagePullBackOff" in reason

    def test_re_enable_after_image_pull_disabled(self) -> None:
        handler = ProjectFileHandler()
        data = self._project_data(disabled=True, reason="ImagePullBackOff: image not found")

        # Simulate what update_image_and_regenerate does
        is_disabled, disabled_reason = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled

        if is_disabled and "ImagePullBackOff" in disabled_reason:
            handler.set_deployment_component_disabled(data, "production", "api", False, "")

        is_disabled, _ = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled is False

    def test_oom_disabled_not_reset(self) -> None:
        """Components disabled for OOM should NOT be re-enabled on image change."""
        handler = ProjectFileHandler()
        data = self._project_data(disabled=True, reason="OOMKilled detected")

        is_disabled, disabled_reason = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled
        assert "ImagePullBackOff" not in disabled_reason

        # The condition would NOT match, so disabled stays true
        is_disabled_after, _ = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled_after is True

    def test_not_disabled_stays_enabled(self) -> None:
        handler = ProjectFileHandler()
        data = self._project_data(disabled=False)

        is_disabled, _ = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled is False


class TestImagePullRemediationUsesReference:
    """Regression: the inline ImagePullBackOff remediation must disable components by
    their user-facing ``component_reference`` (e.g. ``api``), NOT the unique
    deployment-scoped ``component_name`` (e.g. ``production-api``). Disabling looks the
    component up by ``reference``; passing the unique name silently fails to match and the
    component is never scaled to 0, leaving it stuck in ImagePullBackOff and blocking the
    ArgoCD sync (and the pruning of removed components)."""

    def _project_data(self) -> dict:
        return {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [
                        {"reference": "api", "image": "ghcr.io/org/app:main"},
                    ],
                }
            ],
        }

    def _failure(self) -> ComponentFailure:
        # Mirrors what create_health_check_callback emits: component_name is the unique
        # deployment-scoped name, component_reference is the user-facing YAML reference.
        return ComponentFailure(
            component_name="production-api",
            failure_type="image_pull",
            message="ImagePullBackOff: manifest unknown",
            deployment_name="production",
            component_reference="api",
        )

    def test_reference_disables_component(self) -> None:
        """The fixed path (uses component_reference) actually disables the component."""
        handler = ProjectFileHandler()
        data = self._project_data()
        failure = self._failure()

        result = handler.set_deployment_component_disabled(
            data, "production", failure.component_reference, True, failure.message
        )
        assert result is True
        is_disabled, reason = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled is True
        assert "ImagePullBackOff" in reason

    def test_unique_name_does_not_match(self) -> None:
        """The buggy path (uses component_name) fails to match and never disables it."""
        handler = ProjectFileHandler()
        data = self._project_data()
        failure = self._failure()

        result = handler.set_deployment_component_disabled(
            data, "production", failure.component_name, True, failure.message
        )
        assert result is False
        is_disabled, _ = handler.extract_deployment_component_disabled(data, "production", "api")
        assert is_disabled is False
