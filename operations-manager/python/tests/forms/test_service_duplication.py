"""Test that storage services are NOT duplicated in the final project YAML.

Reproduces the bug: browser JSON is clean, but the final project file
has duplicate persistent-storage/temp-storage entries.

Tests the exact _do_submit pipeline:
  yaml_data (merged) → _flatten_yaml_for_validation → apply_to_yaml → YAML output
"""

from __future__ import annotations

import copy
from io import StringIO
from typing import Any

from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from ruamel.yaml import YAML


def _count_service_entries(services: list, service_name: str) -> int:
    """Count how many times a service appears in the mixed list."""
    count = 0
    for item in services:
        if (isinstance(item, str) and item == service_name) or (isinstance(item, dict) and service_name in item):
            count += 1
    return count


def _flatten_yaml_for_validation(
    editables: list[Any],
    yaml_data: dict[str, Any],
) -> dict[str, Any]:
    """Exact copy of the function from router_wizard.py."""
    from opi.forms.editables.path import resolve_path
    from opi.forms.editables.service_path import smart_get_value
    from opi.forms.visualizers.bridge import should_render_editable

    flat: dict[str, Any] = {}

    for editable in editables:
        if not should_render_editable(editable, yaml_data):
            continue

        if str(editable.widget) == "sequence":
            items = smart_get_value(yaml_data, editable.editable.yaml_path) or []
            if not isinstance(items, list):
                continue
            for index in range(len(items)):
                for child in editable.children or []:
                    if not should_render_editable(child, yaml_data):
                        continue
                    if str(child.widget) == "sequence":
                        parent_path = resolve_path(child.editable.yaml_path, index)
                        nested_items = smart_get_value(yaml_data, parent_path) or []
                        if not isinstance(nested_items, list):
                            continue
                        for ci in range(len(nested_items)):
                            for gc in child.children or []:
                                gc_path = resolve_path(gc.editable.yaml_path, index)
                                gc_path = resolve_path(gc_path, ci)
                                flat[gc_path] = smart_get_value(yaml_data, gc_path)
                    else:
                        concrete = resolve_path(child.editable.yaml_path, index)
                        flat[concrete] = smart_get_value(yaml_data, concrete)
        else:
            flat[editable.editable.yaml_path] = smart_get_value(yaml_data, editable.editable.yaml_path)

    return flat


def _to_yaml_string(data: dict) -> str:
    """Serialize dict to YAML string, same as _start_project_creation."""
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    output = StringIO()
    yaml_instance.dump(data, output)
    return output.getvalue()


def _make_merged_data_with_promoted_dicts() -> dict:
    """Merged step data where services already have promoted dicts.

    This is what get_merged_data() returns after the components step
    has been submitted via the JSON pipeline (json-enc.js promoted
    string entries to dicts for {K} filter paths).
    """
    return {
        "components": [
            {
                "name": "frontend",
                "image": "registry.example.com/frontend:latest",
                "services": [
                    "publish-on-web",
                    {
                        "persistent-storage": {
                            "config": [
                                {"name": "data", "size": "1Gi", "mount-path": "/data"},
                            ],
                        },
                    },
                    {
                        "temp-storage": {
                            "config": [
                                {"name": "tmp", "size": "512Mi", "mount-path": "/tmp"},
                            ],
                        },
                    },
                ],
            },
        ],
        "services": ["publish-on-web", "persistent-storage", "temp-storage"],
    }


def _make_merged_data_with_plain_strings() -> dict:
    """Merged data where component services are plain strings.

    This could happen if the services checkbox was stored separately
    from the storage config (different wizard steps).
    """
    return {
        "components": [
            {
                "name": "frontend",
                "image": "registry.example.com/frontend:latest",
                "services": [
                    "publish-on-web",
                    "persistent-storage",
                    "temp-storage",
                ],
            },
        ],
        "services": ["publish-on-web", "persistent-storage", "temp-storage"],
    }


class TestFinalProjectYaml:
    """Test the full _do_submit pipeline using the REAL visualizers."""

    def _run_pipeline(self, yaml_data: dict) -> dict:
        """Run the exact _do_submit pipeline: flatten → apply_to_yaml."""
        editables = [COMPONENTS_SEQUENCE]
        flat = _flatten_yaml_for_validation(editables, yaml_data)
        processor = EditableFormProcessor()
        return processor.apply_to_yaml(flat, editables, yaml_data)

    def test_promoted_dicts_no_duplication(self):
        """Services already promoted to dicts should not be duplicated."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        result = self._run_pipeline(yaml_data)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    def test_plain_strings_no_duplication(self):
        """Services as plain strings — storage config processing should not add dicts."""
        yaml_data = _make_merged_data_with_plain_strings()
        result = self._run_pipeline(yaml_data)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    async def test_json_then_flat_pipeline(self):
        """Full flow: JSON submission → store → merge → flatten → apply → YAML."""
        processor = EditableFormProcessor()
        editables = [COMPONENTS_SEQUENCE]

        # Phase 1: Simulate browser JSON submission (what json-enc.js produces)
        submitted_json = {
            "components": [
                {
                    "name": "frontend",
                    "image": "registry.example.com/frontend:latest",
                    "services": [
                        "publish-on-web",
                        {
                            "persistent-storage": {
                                "config": [
                                    {"name": "data", "size": "1Gi", "mount-path": "/data"},
                                ],
                            },
                        },
                        {
                            "temp-storage": {
                                "config": [
                                    {"name": "tmp", "size": "512Mi", "mount-path": "/tmp"},
                                ],
                            },
                        },
                    ],
                },
            ],
        }

        # yaml_data from previous steps
        initial_yaml = {
            "components": [],
            "services": ["publish-on-web", "persistent-storage", "temp-storage"],
        }

        # JSON pipeline (step submission)
        step_result, errors = await processor.process_json_submission(submitted_json, editables, initial_yaml)

        # Phase 2: Simulate get_merged_data() — extract section keys
        section_keys = {e.editable.yaml_path.split("/")[0].split("[")[0] for e in editables}
        section_data = {k: v for k, v in step_result.items() if k in section_keys}

        # Phase 3: Simulate _do_submit merge
        merged = copy.deepcopy(initial_yaml)
        merged.update(section_data)

        # Phase 4: flatten → apply_to_yaml
        result = self._run_pipeline(merged)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    def test_final_yaml_output(self):
        """Check the actual YAML string output for duplicates."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        result = self._run_pipeline(yaml_data)

        yaml_str = _to_yaml_string(result)

        # Count occurrences in YAML output
        ps_count = yaml_str.count("persistent-storage")
        ts_count = yaml_str.count("temp-storage")

        assert ps_count <= 2, (  # 1 in services list + 1 as dict key = 2 max
            f"persistent-storage appears {ps_count} times in YAML:\n{yaml_str}"
        )
        assert ts_count <= 2, f"temp-storage appears {ts_count} times in YAML:\n{yaml_str}"

    def test_inspect_flat_dict(self):
        """Inspect what _flatten_yaml_for_validation produces — useful for debugging."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        editables = [COMPONENTS_SEQUENCE]
        flat = _flatten_yaml_for_validation(editables, yaml_data)

        # Print flat dict for inspection
        for k, v in sorted(flat.items()):
            print(f"  {k} = {v!r}")

        # The services flat entry should contain the full list
        services_value = flat.get("components[0]/services")
        assert services_value is not None, "components[0]/services missing from flat dict"
        assert isinstance(services_value, list), f"Expected list, got {type(services_value)}"

    def test_inspect_apply_to_yaml_step_by_step(self):
        """Step through apply_to_yaml with logging to find where duplication occurs."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        editables = [COMPONENTS_SEQUENCE]
        flat = _flatten_yaml_for_validation(editables, yaml_data)

        # Deep copy like apply_to_yaml does
        result = copy.deepcopy(yaml_data)
        services_before = result["components"][0]["services"]
        print(f"\nBefore apply_to_yaml: {len(services_before)} services")
        for i, s in enumerate(services_before):
            print(f"  [{i}] {s!r}")

        # Run apply_to_yaml
        processor = EditableFormProcessor()
        result = processor.apply_to_yaml(flat, editables, yaml_data)

        services_after = result["components"][0]["services"]
        print(f"\nAfter apply_to_yaml: {len(services_after)} services")
        for i, s in enumerate(services_after):
            print(f"  [{i}] {s!r}")

        assert len(services_after) == len(services_before), (
            f"Service count changed from {len(services_before)} to {len(services_after)}: {services_after}"
        )

    def test_full_create_flow_all_sections(self):
        """Test with ALL active sections from the CREATE_FLOW — the exact _do_submit setup."""
        from opi.forms.visualizers.flows import CREATE_FLOW

        # Simulate all active sections (services selected, keycloak not)
        active_sections = [
            s
            for s in CREATE_FLOW.sections
            if s.section_id in ("identity", "services", "components", "domain", "deployment")
        ]

        # Collect all editables from active sections (same as _do_submit)
        all_editables = []
        for section in active_sections:
            all_editables.extend(section.editables)

        # Merged data from all steps
        yaml_data = {
            "display-name": "Test Project",
            "description": "A test",
            "clusters": ["local"],
            "services": ["publish-on-web", "persistent-storage", "temp-storage"],
            "components": [
                {
                    "name": "frontend",
                    "image": "registry.example.com/frontend:latest",
                    "services": [
                        "publish-on-web",
                        {
                            "persistent-storage": {
                                "config": [
                                    {"name": "data", "size": "1Gi", "mount-path": "/data"},
                                ],
                            },
                        },
                        {
                            "temp-storage": {
                                "config": [
                                    {"name": "tmp", "size": "512Mi", "mount-path": "/tmp"},
                                ],
                            },
                        },
                    ],
                },
            ],
            "deployments": [{"domain-mode": "nice-url", "subdomain": "test"}],
        }

        flat = _flatten_yaml_for_validation(all_editables, yaml_data)
        processor = EditableFormProcessor()
        result = processor.apply_to_yaml(flat, all_editables, yaml_data)

        # Check component-level services
        comp_services = result["components"][0]["services"]
        print(f"\nComponent services ({len(comp_services)}):")
        for i, s in enumerate(comp_services):
            print(f"  [{i}] {s!r}")

        assert _count_service_entries(comp_services, "persistent-storage") == 1, (
            f"persistent-storage duplicated in component! services = {comp_services}"
        )
        assert _count_service_entries(comp_services, "temp-storage") == 1, (
            f"temp-storage duplicated in component! services = {comp_services}"
        )

        # Check project-level services
        proj_services = result.get("services", [])
        print(f"\nProject services ({len(proj_services)}):")
        for i, s in enumerate(proj_services):
            print(f"  [{i}] {s!r}")

        assert _count_service_entries(proj_services, "persistent-storage") == 1, (
            f"persistent-storage duplicated in project! services = {proj_services}"
        )

        # Check YAML output
        yaml_str = _to_yaml_string(result)
        print(f"\nFinal YAML:\n{yaml_str}")
