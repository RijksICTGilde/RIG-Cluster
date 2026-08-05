"""Tests for opi.web.router_detail_edit helper functions."""

from __future__ import annotations

from opi.forms.wizard.save import apply_list_item_merge as _apply_list_item_merge
from opi.forms.wizard.save import detect_list_target as _detect_list_target
from opi.web.router_detail_edit import _seed_components_for_new_deployment


class TestDetectListTarget:
    """Tests for _detect_list_target flow-id parsing."""

    def test_add_deployment(self) -> None:
        result = _detect_list_target("modal-add-deployment-2", state=None)
        assert result == ("deployments", 2, True)

    def test_edit_deployment(self) -> None:
        result = _detect_list_target("modal-edit-deployment-0", state=None)
        assert result == ("deployments", 0, False)

    def test_edit_component(self) -> None:
        result = _detect_list_target("modal-edit-component-1", state=None)
        # is_new is None when state is None (falsy short-circuit)
        assert result is not None
        assert result[0] == "components"
        assert result[1] == 1
        assert not result[2]  # falsy (None when state is None)

    def test_edit_domain(self) -> None:
        result = _detect_list_target("modal-edit-domain-0", state=None)
        assert result == ("deployments", 0, False)

    def test_edit_backup_schedule(self) -> None:
        result = _detect_list_target("modal-edit-backup-schedule-0", state=None)
        assert result == ("deployments", 0, False)

    def test_edit_backup_schedule_second_deployment(self) -> None:
        result = _detect_list_target("modal-edit-backup-schedule-1", state=None)
        assert result == ("deployments", 1, False)

    def test_unknown_flow(self) -> None:
        assert _detect_list_target("modal-edit-services-0", state=None) is None

    def test_non_numeric_suffix(self) -> None:
        assert _detect_list_target("modal-add-deployment-abc", state=None) is None


class TestApplyListItemMerge:
    """Tests for _apply_list_item_merge."""

    def test_add_new_deployment(self) -> None:
        existing: dict = {"deployments": [{"name": "staging", "cluster": "prod"}]}
        merged: dict = {"deployments": [{}, {"name": "kopie", "domain-mode": "nice-url"}]}
        _apply_list_item_merge(existing, merged, "deployments", idx=1, is_new=True)
        assert len(existing["deployments"]) == 2
        assert existing["deployments"][1]["name"] == "kopie"

    def test_edit_existing_deployment(self) -> None:
        existing: dict = {"deployments": [{"name": "staging", "cluster": "prod", "namespace": "myproj"}]}
        merged: dict = {"deployments": [{"domain-mode": "nice-url"}]}
        _apply_list_item_merge(existing, merged, "deployments", idx=0, is_new=False)
        assert existing["deployments"][0]["name"] == "staging"
        assert existing["deployments"][0]["domain-mode"] == "nice-url"
        assert existing["deployments"][0]["cluster"] == "prod"

    def test_add_creates_list_if_missing(self) -> None:
        existing: dict = {}
        merged: dict = {"deployments": [{"name": "new"}]}
        _apply_list_item_merge(existing, merged, "deployments", idx=0, is_new=True)
        assert existing["deployments"] == [{"name": "new"}]


class TestNewDeploymentSystemFields:
    """Test that new deployments get namespace/cluster/repository injected.

    This tests the logic added in _modal_do_submit after _apply_list_item_merge.
    We replicate the logic here since _modal_do_submit is async and needs request objects.
    """

    @staticmethod
    def _inject_system_fields(existing_data: dict, project_name: str) -> None:
        """Replicate the system field injection from _modal_do_submit."""
        deployments = existing_data.get("deployments", [])
        if deployments and isinstance(deployments[-1], dict):
            new_dep = deployments[-1]
            existing_dep = next(
                (d for d in deployments[:-1] if isinstance(d, dict)),
                None,
            )
            new_dep.setdefault("namespace", project_name)
            if existing_dep:
                for field in ("cluster", "repository"):
                    if field in existing_dep and field not in new_dep:
                        new_dep[field] = existing_dep[field]

    def test_namespace_set_from_project_name(self) -> None:
        existing = {
            "deployments": [
                {"name": "staging", "cluster": "sandboxed-local", "namespace": "myproj", "repository": "main-repo"},
                {"name": "kopie", "domain-mode": "nice-url"},
            ],
        }
        self._inject_system_fields(existing, "myproj")
        new_dep = existing["deployments"][1]
        assert new_dep["namespace"] == "myproj"
        assert new_dep["cluster"] == "sandboxed-local"
        assert new_dep["repository"] == "main-repo"

    def test_namespace_not_overwritten_if_already_set(self) -> None:
        existing = {
            "deployments": [
                {"name": "staging", "namespace": "proj-a"},
                {"name": "kopie", "namespace": "custom-ns"},
            ],
        }
        self._inject_system_fields(existing, "proj-a")
        assert existing["deployments"][1]["namespace"] == "custom-ns"

    def test_no_existing_deployment_still_sets_namespace(self) -> None:
        existing = {"deployments": [{"name": "first"}]}
        self._inject_system_fields(existing, "myproj")
        assert existing["deployments"][0]["namespace"] == "myproj"

    def test_empty_deployments_is_noop(self) -> None:
        existing: dict = {"deployments": []}
        self._inject_system_fields(existing, "myproj")
        assert existing["deployments"] == []


class _FakeState:
    """Minimal state mock for _seed_components_for_new_deployment tests."""

    def __init__(self, template_data: dict, step_data: dict | None = None) -> None:
        self.template_data = template_data
        self.step_data: dict = step_data or {}
        self.active_sections: list[str] = list(self.step_data.keys())

    def get_merged_data(self) -> dict:
        import copy

        merged: dict = {}
        if self.template_data:
            merged.update(copy.deepcopy(self.template_data))
        for section_id in self.active_sections:
            if section_id not in self.step_data:
                continue
            for key, value in self.step_data[section_id].items():
                if key in merged and isinstance(merged[key], list) and isinstance(value, list):
                    target = merged[key]
                    for i, src_item in enumerate(value):
                        if i < len(target):
                            if isinstance(target[i], dict) and isinstance(src_item, dict):
                                target[i].update(copy.deepcopy(src_item))
                            else:
                                target[i] = copy.deepcopy(src_item)
                        else:
                            target.append(copy.deepcopy(src_item))
                else:
                    merged[key] = copy.deepcopy(value)
        return merged

    def store_step_data(self, section_id: str, data: dict) -> None:
        self.step_data[section_id] = data
        if section_id not in self.active_sections:
            self.active_sections.append(section_id)


class TestSeedComponentsForNewDeployment:
    """Tests for _seed_components_for_new_deployment."""

    def test_clone_from_string_fills_images_from_source(self) -> None:
        state = _FakeState(
            template_data={
                "components": [
                    {"name": "frontend"},
                    {"name": "api"},
                ],
                "deployments": [
                    {
                        "name": "staging",
                        "components": [
                            {"reference": "frontend", "image": "nginx:latest"},
                            {"reference": "api", "image": "python:3.13"},
                        ],
                    },
                    {"cluster": "sandboxed-local"},
                ],
            },
            step_data={
                "add-deployment-info-1": {
                    "deployments": [{}, {"name": "kopie", "clone-from": "staging"}],
                },
            },
        )
        _seed_components_for_new_deployment(state, 1)
        seeded = state.step_data.get("add-deployment-components-1", {})
        components = seeded.get("deployments", [{}])[1].get("components", [])
        assert len(components) == 2
        assert components[0] == {"reference": "frontend", "image": "nginx:latest"}
        assert components[1] == {"reference": "api", "image": "python:3.13"}

    def test_clone_from_dict_fills_images_from_source(self) -> None:
        """After the converter, clone-from is a dict with 'reference' key."""
        state = _FakeState(
            template_data={
                "components": [
                    {"name": "frontend"},
                    {"name": "api"},
                ],
                "deployments": [
                    {
                        "name": "staging",
                        "components": [
                            {"reference": "frontend", "image": "nginx:latest"},
                            {"reference": "api", "image": "python:3.13"},
                        ],
                    },
                    {"cluster": "sandboxed-local"},
                ],
            },
            step_data={
                "add-deployment-info-1": {
                    "deployments": [
                        {},
                        {
                            "name": "kopie",
                            "clone-from": {"type": "deployment", "reference": "staging", "mode": "once"},
                        },
                    ],
                },
            },
        )
        _seed_components_for_new_deployment(state, 1)
        seeded = state.step_data.get("add-deployment-components-1", {})
        components = seeded.get("deployments", [{}])[1].get("components", [])
        assert len(components) == 2
        assert components[0] == {"reference": "frontend", "image": "nginx:latest"}
        assert components[1] == {"reference": "api", "image": "python:3.13"}

    def test_no_clone_from_seeds_all_project_components_with_empty_images(self) -> None:
        state = _FakeState(
            template_data={
                "components": [
                    {"name": "frontend", "image": "nginx:latest"},
                    {"name": "api", "image": "python:3.13"},
                ],
                "deployments": [
                    {"name": "staging", "components": [{"reference": "frontend", "image": "nginx:latest"}]},
                    {"cluster": "sandboxed-local"},
                ],
            },
            step_data={
                "add-deployment-info-1": {
                    "deployments": [{}, {"name": "kopie"}],
                },
            },
        )
        _seed_components_for_new_deployment(state, 1)
        seeded = state.step_data.get("add-deployment-components-1", {})
        components = seeded.get("deployments", [{}])[1].get("components", [])
        assert len(components) == 2
        assert components[0] == {"reference": "frontend", "image": ""}
        assert components[1] == {"reference": "api", "image": ""}

    def test_clone_from_nonexistent_seeds_empty_images(self) -> None:
        """Unknown source still seeds all components, just with empty images."""
        state = _FakeState(
            template_data={
                "components": [{"name": "app"}],
                "deployments": [
                    {"name": "staging", "components": [{"reference": "app", "image": "img:1"}]},
                    {"cluster": "sandboxed-local"},
                ],
            },
            step_data={
                "add-deployment-info-1": {
                    "deployments": [{}, {"name": "kopie", "clone-from": "nonexistent"}],
                },
            },
        )
        _seed_components_for_new_deployment(state, 1)
        seeded = state.step_data.get("add-deployment-components-1", {})
        components = seeded.get("deployments", [{}])[1].get("components", [])
        assert len(components) == 1
        assert components[0] == {"reference": "app", "image": ""}

    def test_out_of_range_index_does_nothing(self) -> None:
        state = _FakeState(template_data={"deployments": []})
        _seed_components_for_new_deployment(state, 5)
        assert len(state.step_data) == 0
