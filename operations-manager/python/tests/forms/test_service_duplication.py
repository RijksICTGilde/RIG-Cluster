"""Test that storage services are NOT duplicated in the final project YAML.

Reproduces the bug: browser JSON is clean, but the final project file
has duplicate persistent-storage/temp-storage entries.

Tests the pipeline:
  yaml_data → process_json_submission → YAML output
"""

from __future__ import annotations

import copy
from io import StringIO

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
    """Test the full pipeline using the REAL visualizers."""

    async def _run_pipeline(self, yaml_data: dict) -> dict:
        """Run the pipeline: process_json_submission."""
        editables = [COMPONENTS_SEQUENCE]
        processor = EditableFormProcessor()
        result, _errors = await processor.process_json_submission(yaml_data, editables, yaml_data)
        return result

    async def test_promoted_dicts_no_duplication(self):
        """Services already promoted to dicts should not be duplicated."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        result = await self._run_pipeline(yaml_data)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    async def test_plain_strings_no_duplication(self):
        """Services as plain strings — storage config processing should not add dicts."""
        yaml_data = _make_merged_data_with_plain_strings()
        result = await self._run_pipeline(yaml_data)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    async def test_json_then_flat_pipeline(self):
        """Full flow: JSON submission → store → merge → process_json_submission → YAML."""
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

        # Phase 4: process_json_submission
        result = await self._run_pipeline(merged)

        services = result["components"][0]["services"]
        assert _count_service_entries(services, "persistent-storage") == 1, (
            f"persistent-storage duplicated! services = {services}"
        )
        assert _count_service_entries(services, "temp-storage") == 1, f"temp-storage duplicated! services = {services}"

    async def test_final_yaml_output(self):
        """Check the actual YAML string output for duplicates."""
        yaml_data = _make_merged_data_with_promoted_dicts()
        result = await self._run_pipeline(yaml_data)

        yaml_str = _to_yaml_string(result)

        # Count occurrences in YAML output
        ps_count = yaml_str.count("persistent-storage")
        ts_count = yaml_str.count("temp-storage")

        assert ps_count <= 2, (  # 1 in services list + 1 as dict key = 2 max
            f"persistent-storage appears {ps_count} times in YAML:\n{yaml_str}"
        )
        assert ts_count <= 2, f"temp-storage appears {ts_count} times in YAML:\n{yaml_str}"

    async def test_full_create_flow_all_sections(self):
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

        processor = EditableFormProcessor()
        result, _errors = await processor.process_json_submission(yaml_data, all_editables, yaml_data)

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
