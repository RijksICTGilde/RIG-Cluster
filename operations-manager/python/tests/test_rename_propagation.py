"""Tests for rename propagation in EditableFormProcessor."""

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.visualizer import EditableVisualizer


def _make_name_editable(index: int) -> EditableVisualizer:
    """Create a materialized component name editable at a specific index."""
    return EditableVisualizer(
        editable=Editable(
            yaml_path=f"components[{index}]/name",
            rename_targets=[
                "deployments[*]/components[*]/reference",
                "components[*]/uses-components",
            ],
        ),
        widget=WidgetType.TEXT,
        label="Name",
    )


class TestApplyRename:
    def test_direct_string_field(self):
        data = {
            "deployments": [
                {"components": [{"reference": "frontend"}, {"reference": "backend"}]},
                {"components": [{"reference": "frontend"}]},
            ]
        }
        processor = EditableFormProcessor()
        count = processor._apply_rename(data, "deployments[*]/components[*]/reference", "frontend", "web-app")
        assert count == 2
        assert data["deployments"][0]["components"][0]["reference"] == "web-app"
        assert data["deployments"][0]["components"][1]["reference"] == "backend"
        assert data["deployments"][1]["components"][0]["reference"] == "web-app"

    def test_list_membership(self):
        data = {
            "components": [
                {"name": "web-app", "uses-components": ["backend", "worker"]},
                {"name": "backend", "uses-components": ["frontend"]},
            ]
        }
        processor = EditableFormProcessor()
        count = processor._apply_rename(data, "components[*]/uses-components", "frontend", "web-app")
        assert count == 1
        assert data["components"][0]["uses-components"] == ["backend", "worker"]
        assert data["components"][1]["uses-components"] == ["web-app"]

    def test_no_matches(self):
        data = {
            "deployments": [
                {"components": [{"reference": "backend"}]},
            ]
        }
        processor = EditableFormProcessor()
        count = processor._apply_rename(data, "deployments[*]/components[*]/reference", "frontend", "web-app")
        assert count == 0

    def test_missing_path(self):
        data = {"other": "data"}
        processor = EditableFormProcessor()
        count = processor._apply_rename(data, "deployments[*]/components[*]/reference", "frontend", "web-app")
        assert count == 0


class TestPropagateRenames:
    def test_full_rename_propagation(self):
        original = {
            "components": [
                {"name": "frontend", "uses-components": []},
                {"name": "backend", "uses-components": ["frontend"]},
            ],
            "deployments": [
                {"components": [{"reference": "frontend"}, {"reference": "backend"}]},
            ],
        }
        result = {
            "components": [
                {"name": "web-app", "uses-components": []},
                {"name": "backend", "uses-components": ["frontend"]},
            ],
            "deployments": [
                {"components": [{"reference": "frontend"}, {"reference": "backend"}]},
            ],
        }
        editables = [_make_name_editable(0), _make_name_editable(1)]

        processor = EditableFormProcessor()
        renames = processor.propagate_renames(original, result, editables)

        assert len(renames) == 2  # 2 target paths
        # Deployment references updated
        assert result["deployments"][0]["components"][0]["reference"] == "web-app"
        assert result["deployments"][0]["components"][1]["reference"] == "backend"
        # Cross-component uses-components updated
        assert result["components"][1]["uses-components"] == ["web-app"]

    def test_no_rename_when_unchanged(self):
        data = {
            "components": [{"name": "frontend"}],
            "deployments": [{"components": [{"reference": "frontend"}]}],
        }
        editables = [_make_name_editable(0)]

        processor = EditableFormProcessor()
        renames = processor.propagate_renames(data, data, editables)
        assert renames == []

    def test_skip_editables_without_rename_targets(self):
        original = {"components": [{"name": "old"}]}
        result = {"components": [{"name": "new"}]}

        # Editable without rename_targets
        editable = EditableVisualizer(
            editable=Editable(yaml_path="components[0]/name"),
            widget=WidgetType.TEXT,
            label="Name",
        )

        processor = EditableFormProcessor()
        renames = processor.propagate_renames(original, result, [editable])
        assert renames == []
