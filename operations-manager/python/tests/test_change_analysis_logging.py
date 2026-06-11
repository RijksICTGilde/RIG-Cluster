"""Tests for change-analysis log labels and component-extraction log dedupe.

Change analysis used to log opaque DeepDiff paths ("deployments.7"); it now
resolves them to deployment names. extract_deployment_components used to emit
an identical DEBUG line on every call (dozens per run); it now logs once per
deployment per handler instance.
"""

import logging

from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import (
    _deployment_name_for_path,
    _deployment_names_for_paths,
)

YAML_DOC = {
    "deployments": [
        {"name": "main"},
        {"name": "pr-274"},
        {"name": "pr-372"},
    ]
}


class TestDeploymentNameForPath:
    def test_resolves_top_level_path(self):
        assert _deployment_name_for_path("deployments.1", YAML_DOC) == "pr-274"

    def test_resolves_nested_path(self):
        assert _deployment_name_for_path("deployments.0.components.0.image", YAML_DOC) == "main"

    def test_out_of_range_index_returns_none(self):
        assert _deployment_name_for_path("deployments.9", YAML_DOC) is None

    def test_non_deployment_path_returns_none(self):
        assert _deployment_name_for_path("components.0", YAML_DOC) is None
        assert _deployment_name_for_path("deployments", YAML_DOC) is None

    def test_missing_yaml_returns_none(self):
        assert _deployment_name_for_path("deployments.0", None) is None


class TestDeploymentNamesForPaths:
    def test_resolves_and_dedupes(self):
        paths = ["deployments.0", "deployments.0.components", "deployments.2"]
        assert _deployment_names_for_paths(paths, YAML_DOC) == ["main", "pr-372"]

    def test_unresolvable_path_falls_back_to_raw_path(self):
        assert _deployment_names_for_paths(["deployments.9"], YAML_DOC) == ["deployments.9"]


class TestComponentExtractionLogDedupe:
    def test_logs_once_per_deployment(self, caplog):
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [{"name": "main", "components": [{"reference": "frontend"}, {"reference": "worker"}]}]
        }

        with caplog.at_level(logging.DEBUG, logger="opi.handlers.project_file_handler"):
            for _ in range(5):
                components = handler.extract_deployment_components(project_data, "main")
                assert len(components) == 2

        found_lines = [r for r in caplog.records if "component reference(s) in deployment 'main'" in r.message]
        assert len(found_lines) == 1

    def test_different_deployments_each_log(self, caplog):
        handler = ProjectFileHandler()
        project_data = {
            "deployments": [
                {"name": "main", "components": [{"reference": "frontend"}]},
                {"name": "pr-274", "components": [{"reference": "frontend"}]},
            ]
        }

        with caplog.at_level(logging.DEBUG, logger="opi.handlers.project_file_handler"):
            handler.extract_deployment_components(project_data, "main")
            handler.extract_deployment_components(project_data, "pr-274")

        messages = [r.message for r in caplog.records if "component reference(s)" in r.message]
        assert len(messages) == 2
