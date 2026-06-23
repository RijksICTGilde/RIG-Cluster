"""Non-destructive edit tests using real project YAML files.

Every modal edit flow that targets a list item (deployments, components)
MUST preserve all existing project data. These tests load a real project
file, simulate what _modal_do_submit does, and verify that only the
intended field changed while everything else is preserved.

This catches the class of bug where a new flow ID prefix is not registered
in _detect_list_target, causing the save to replace the entire project.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from opi.forms.wizard.state import CLEARED_FIELD
from opi.web.router_detail_edit import (
    _apply_list_item_merge,
    _detect_list_target,
)

FIXTURES_DIR = Path(__file__).parent / "e2e" / "fixtures" / "projects"
UNIT_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"
PROJECTS_DIR = Path(__file__).parent.parent.parent.parent / "projects"


def _load_project(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _collect_project_files() -> list[Path]:
    """Collect all available project YAML fixtures."""
    files = []
    for d in [FIXTURES_DIR, UNIT_FIXTURES_DIR, PROJECTS_DIR]:
        if d.exists():
            files.extend(d.glob("*.yaml"))
    return files


def _simulate_modal_save(
    existing_data: dict,
    merged_data: dict,
    flow_id: str,
) -> dict:
    """Simulate the merge logic from _modal_do_submit.

    Returns the resulting project data after the simulated save.
    This replicates the exact logic: detect list target, apply list
    item merge or fall through to update.
    """
    result = copy.deepcopy(existing_data)
    merged = copy.deepcopy(merged_data)

    list_target = _detect_list_target(flow_id, state=None)
    if list_target:
        list_key, idx, is_new = list_target
        _apply_list_item_merge(result, merged, list_key, idx, is_new)
        merged.pop(list_key, None)

    result.update(merged)
    return result


# ---------------------------------------------------------------------------
# Parametrized non-destructive tests for every deployment-targeting flow
# ---------------------------------------------------------------------------

DEPLOYMENT_FLOW_CASES = [
    (
        "modal-edit-deployment-0",
        {"deployments": [{"domain-format": "component-deployment-project"}]},
        "domain-format",
    ),
    (
        "modal-edit-domain-0",
        {"deployments": [{"domain-format": "project"}]},
        "domain-format",
    ),
    (
        "modal-edit-backup-schedule-0",
        {"deployments": [{"backup": {"schedule": "daily"}}]},
        "backup",
    ),
]


class TestNonDestructiveDeploymentEdits:
    """Verify that editing a single deployment field preserves all other data."""

    @pytest.fixture
    def project_data(self) -> dict:
        """A realistic project with multiple top-level keys and deployments."""
        return {
            "name": "test-project",
            "display-name": "Test Project",
            "description": "A test project",
            "clusters": ["local"],
            "services": ["publish-on-web", "keycloak"],
            "users": [{"email": "admin@test.nl", "role": "admin"}],
            "config": {"age-public-key": "age1abc123"},
            "components": [
                {"name": "web", "publish-on-web": True, "type": "deployment"},
                {"name": "worker", "type": "deployment"},
            ],
            "repositories": [
                {"name": "main", "url": "ssh://git@host/repo.git", "branch": "main"},
            ],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "local",
                    "namespace": "test-project",
                    "repository": "main",
                    "domain-format": "component-deployment-project",
                    "components": [
                        {"reference": "web", "image": "nginx:latest"},
                        {"reference": "worker", "image": "python:3.13"},
                    ],
                },
                {
                    "name": "staging",
                    "cluster": "local",
                    "namespace": "test-project",
                    "repository": "main",
                    "components": [
                        {"reference": "web", "image": "nginx:1.25"},
                    ],
                },
            ],
        }

    @pytest.mark.parametrize(("flow_id", "merged_data", "changed_key"), DEPLOYMENT_FLOW_CASES)
    def test_edit_preserves_all_other_fields(
        self,
        project_data: dict,
        flow_id: str,
        merged_data: dict,
        changed_key: str,
    ) -> None:
        """The edit must only modify the targeted field; everything else stays."""
        original = copy.deepcopy(project_data)
        result = _simulate_modal_save(project_data, merged_data, flow_id)

        # Top-level keys outside deployments must be untouched
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # All deployments must still exist
        assert len(result["deployments"]) == len(original["deployments"])

        # The targeted deployment (index 0) must keep all its other fields
        original_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in original_dep:
            if key == changed_key:
                continue
            assert result_dep[key] == original_dep[key], f"Deployment field '{key}' was modified by {flow_id}"

        # The non-targeted deployment must be completely untouched
        assert result["deployments"][1] == original["deployments"][1]

    def test_backup_schedule_on_second_deployment(self, project_data: dict) -> None:
        """Setting backup schedule on deployment[1] must not touch deployment[0]."""
        original = copy.deepcopy(project_data)
        merged = {"deployments": [{}, {"backup": {"schedule": "weekly"}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-backup-schedule-1")

        # First deployment untouched
        assert result["deployments"][0] == original["deployments"][0]

        # Second deployment gets backup, keeps everything else
        assert result["deployments"][1]["backup"] == {"schedule": "weekly"}
        assert result["deployments"][1]["name"] == "staging"
        assert result["deployments"][1]["cluster"] == "local"
        assert result["deployments"][1]["components"] == original["deployments"][1]["components"]


class TestNonDestructiveComponentEdits:
    """Verify that editing a component preserves all other data."""

    @pytest.fixture
    def project_data(self) -> dict:
        return {
            "name": "test-project",
            "components": [
                {"name": "web", "publish-on-web": True, "type": "deployment", "ports": {"inbound": [8080]}},
                {"name": "worker", "type": "deployment"},
            ],
            "deployments": [
                {"name": "prod", "cluster": "local", "components": [{"reference": "web", "image": "nginx:latest"}]},
            ],
        }

    def test_edit_component_preserves_project(self, project_data: dict) -> None:
        original = copy.deepcopy(project_data)
        merged = {"components": [{"ports": {"inbound": [9090]}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-component-0")

        # Component 0 updated, but keeps other fields
        assert result["components"][0]["ports"] == {"inbound": [9090]}
        assert result["components"][0]["name"] == "web"
        assert result["components"][0]["publish-on-web"] is True

        # Component 1 untouched
        assert result["components"][1] == original["components"][1]

        # Deployments untouched
        assert result["deployments"] == original["deployments"]


class TestClearedFieldEdits:
    """Clearing a field in a component/deployment edit must delete it.

    A plain dict.update cannot express a deleted key, so the wizard carries
    a CLEARED_FIELD tombstone for fields the user emptied (built with
    get_merged_data(strip_cleared=False)). _apply_list_item_merge must drop
    the tombstoned key instead of resurrecting the old value.
    """

    @pytest.fixture
    def project_data(self) -> dict:
        return {
            "name": "test-project",
            "components": [
                {
                    "name": "web",
                    "type": "deployment",
                    "ports": {"inbound": [8080]},
                    "aliases": {"REDIS_HOSTS": "redis://web.redis:6379"},
                },
                {"name": "worker", "type": "deployment"},
            ],
            "deployments": [
                {"name": "prod", "cluster": "local", "components": [{"reference": "web", "image": "nginx:latest"}]},
            ],
        }

    def test_clearing_aliases_removes_the_key(self, project_data: dict) -> None:
        original = copy.deepcopy(project_data)
        # Tombstone on aliases = user emptied the aliases field.
        merged = {"components": [{"aliases": CLEARED_FIELD, "ports": {"inbound": [8080]}}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-component-0")

        assert "aliases" not in result["components"][0], "Cleared aliases was resurrected"
        # Untouched fields survive, tombstone never reaches the saved data.
        assert result["components"][0]["name"] == "web"
        assert result["components"][0]["ports"] == {"inbound": [8080]}
        assert CLEARED_FIELD not in result["components"][0].values()
        # Sibling component untouched.
        assert result["components"][1] == original["components"][1]

    def test_clearing_deployment_field_removes_the_key(self, project_data: dict) -> None:
        project_data["deployments"][0]["domain-format"] = "project"
        merged = {"deployments": [{"domain-format": CLEARED_FIELD}]}
        result = _simulate_modal_save(project_data, merged, "modal-edit-deployment-0")

        assert "domain-format" not in result["deployments"][0], "Cleared deployment field was resurrected"
        assert result["deployments"][0]["name"] == "prod"


class TestUnregisteredFlowDetection:
    """Verify that _detect_list_target covers all deployment-targeting flows."""

    EXPECTED_DEPLOYMENT_PREFIXES: ClassVar[list[str]] = [
        "modal-edit-deployment-",
        "modal-add-deployment-",
        "modal-edit-domain-",
        "modal-edit-backup-schedule-",
    ]

    EXPECTED_COMPONENT_PREFIXES: ClassVar[list[str]] = [
        "modal-edit-component-",
    ]

    @pytest.mark.parametrize("prefix", EXPECTED_DEPLOYMENT_PREFIXES)
    def test_deployment_prefix_is_registered(self, prefix: str) -> None:
        result = _detect_list_target(f"{prefix}0", state=None)
        assert result is not None, f"Flow prefix '{prefix}' not registered in _detect_list_target"
        assert result[0] == "deployments"

    @pytest.mark.parametrize("prefix", EXPECTED_COMPONENT_PREFIXES)
    def test_component_prefix_is_registered(self, prefix: str) -> None:
        result = _detect_list_target(f"{prefix}0", state=None)
        assert result is not None, f"Flow prefix '{prefix}' not registered in _detect_list_target"
        assert result[0] == "components"


class TestAgainstRealProjectFiles:
    """Run non-destructive checks against every real project YAML fixture.

    This ensures that simulating a backup schedule save on any real
    project file does not lose data.
    """

    @staticmethod
    def _project_files() -> list[Path]:
        return _collect_project_files()

    @pytest.fixture(params=_collect_project_files(), ids=lambda p: p.name)
    def real_project(self, request: pytest.FixtureRequest) -> dict:
        return _load_project(request.param)

    def test_backup_schedule_preserves_all_data(self, real_project: dict) -> None:
        """Setting a backup schedule on deployment 0 must not lose any data."""
        if not real_project.get("deployments"):
            pytest.skip("No deployments in project file")

        original = copy.deepcopy(real_project)
        merged = {"deployments": [{"backup": {"schedule": "daily"}}]}
        result = _simulate_modal_save(real_project, merged, "modal-edit-backup-schedule-0")

        # Every top-level key must still exist
        for key in original:
            assert key in result, f"Top-level key '{key}' was deleted"

        # Every top-level key except deployments must be identical
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # Same number of deployments
        assert len(result["deployments"]) == len(original["deployments"])

        # Non-targeted deployments are identical
        for i in range(1, len(original["deployments"])):
            assert result["deployments"][i] == original["deployments"][i], f"Deployment {i} was modified"

        # Targeted deployment keeps all original fields
        orig_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in orig_dep:
            if key == "backup":
                continue
            assert key in result_dep, f"Deployment field '{key}' was deleted"
            assert result_dep[key] == orig_dep[key], f"Deployment field '{key}' was modified"

        # The backup schedule was set by the merge
        assert result_dep.get("backup") == {"schedule": "daily"}

    def test_domain_edit_preserves_all_data(self, real_project: dict) -> None:
        """Changing domain-format on deployment 0 must not lose any data."""
        if not real_project.get("deployments"):
            pytest.skip("No deployments in project file")

        original = copy.deepcopy(real_project)
        merged = {"deployments": [{"domain-format": "project"}]}
        result = _simulate_modal_save(real_project, merged, "modal-edit-domain-0")

        # Every top-level key except deployments must be identical
        for key in original:
            if key == "deployments":
                continue
            assert result[key] == original[key], f"Top-level key '{key}' was modified"

        # Same number of deployments
        assert len(result["deployments"]) == len(original["deployments"])

        # Targeted deployment keeps all original fields
        orig_dep = original["deployments"][0]
        result_dep = result["deployments"][0]
        for key in orig_dep:
            if key == "domain-format":
                continue
            assert result_dep[key] == orig_dep[key], f"Deployment field '{key}' was modified"
