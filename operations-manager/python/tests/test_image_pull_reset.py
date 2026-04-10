"""Tests for re-enabling components disabled by ImagePullBackOff on image update."""

from opi.handlers.project_file_handler import ProjectFileHandler


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
