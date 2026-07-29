"""Tests for stable field order on modal saves (Bevinding C).

The sequence processor prunes the fields a section manages and merges the submission back
over the original; a pop-then-reassign moves those keys to the end, so every save rotated
reference/image/services/resources and inflated the diff. _reorder_like restores the
pre-edit order in place (preserving ruamel comments/anchors), keeping the diff to the actual
change.
"""

from __future__ import annotations

import difflib

import pytest
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.processor import EditableFormProcessor, _reorder_like
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string


class TestReorderLike:
    def test_restores_original_order_in_place(self):
        original = {"reference": "a", "image": "i", "services": ["s"], "resources": {"cpu": "1"}}
        # merged has the managed keys pushed to the end (the churn the fix targets).
        merged = {"reference": "a", "services": ["s"], "resources": {"cpu": "1"}, "image": "i2"}
        _reorder_like(original, merged)
        assert list(merged.keys()) == ["reference", "image", "services", "resources"]

    def test_new_keys_go_last(self):
        original = {"a": 1, "b": 2}
        merged = {"b": 2, "c": 3, "a": 1}
        _reorder_like(original, merged)
        assert list(merged.keys()) == ["a", "b", "c"]

    def test_recurses_into_nested_dicts(self):
        original = {"outer": {"x": 1, "y": 2}}
        merged = {"outer": {"y": 2, "x": 1}}
        _reorder_like(original, merged)
        assert list(merged["outer"].keys()) == ["x", "y"]

    def test_preserves_comments_on_commented_map(self):
        data = load_yaml_from_string("root:\n  reference: a  # keep me\n  image: i\n  services:\n    - s\n")
        item = data["root"]
        # Simulate the churn: pop + reassign pushes image to the end.
        item["image"] = item.pop("image")
        _reorder_like({"reference": "a", "image": "i", "services": ["s"]}, item)
        assert list(item.keys()) == ["reference", "image", "services"]
        assert "keep me" in dump_yaml_to_string(data)

    def test_non_dicts_returned_unchanged(self):
        assert _reorder_like("a", "b") == "b"
        assert _reorder_like({"a": 1}, ["x"]) == ["x"]


_PROJECT_YAML = """name: proj
components:
  - name: comp-a
    image: img:1
    services:
      - publish-on-web
    resources:
      requests:
        memory: 128Mi
      limits:
        memory: 128Mi
"""


@pytest.mark.asyncio
async def test_editing_one_field_changes_exactly_one_line():
    """A modal save that only changes image must not rotate the other component fields."""
    yaml_data = load_yaml_from_string(_PROJECT_YAML)
    before = dump_yaml_to_string(yaml_data)

    processor = EditableFormProcessor()
    image_vis = EditableVisualizer(
        editable=Editable(yaml_path="components[*]/image"),
        widget=WidgetType.TEXT,
        label="Image",
    )
    seq_vis = EditableVisualizer(
        editable=Editable(yaml_path="components"),
        widget=WidgetType.SEQUENCE,
        label="Components",
        children=[image_vis],
    )
    submitted = {
        "components": [
            {
                "name": "comp-a",
                "image": "img:2",
                "services": ["publish-on-web"],
                "resources": {"requests": {"memory": "128Mi"}, "limits": {"memory": "128Mi"}},
            }
        ]
    }

    result, errors = await processor.process_json_submission(submitted, [seq_vis], yaml_data)
    assert not errors
    after = dump_yaml_to_string(result)

    # The only line that differs is the image line - no reordering churn.
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert changed == ["-    image: img:1", "+    image: img:2"], changed
