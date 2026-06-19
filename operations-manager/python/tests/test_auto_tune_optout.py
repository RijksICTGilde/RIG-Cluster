"""Tests for the auto-tune opt-out flag extraction."""

from opi.handlers.project_file_handler import ProjectFileHandler


def _project(component_flag=None, deployment_flag=None):
    comp_def = {"name": "api"}
    if component_flag is not None:
        comp_def["auto-tune-resources"] = component_flag
    dep_comp = {"reference": "api"}
    if deployment_flag is not None:
        dep_comp["auto-tune-resources"] = deployment_flag
    return {
        "components": [comp_def],
        "deployments": [{"name": "production", "components": [dep_comp]}],
    }


class TestExtractAutoTuneEnabled:
    def test_default_enabled(self):
        handler = ProjectFileHandler()
        assert handler.extract_auto_tune_enabled(_project(), "production", "api") is True

    def test_component_opt_out(self):
        handler = ProjectFileHandler()
        assert handler.extract_auto_tune_enabled(_project(component_flag=False), "production", "api") is False

    def test_deployment_override_wins_over_component(self):
        handler = ProjectFileHandler()
        # Component says disabled, deployment override re-enables.
        data = _project(component_flag=False, deployment_flag=True)
        assert handler.extract_auto_tune_enabled(data, "production", "api") is True

    def test_deployment_opt_out_over_component_enabled(self):
        handler = ProjectFileHandler()
        data = _project(component_flag=True, deployment_flag=False)
        assert handler.extract_auto_tune_enabled(data, "production", "api") is False
