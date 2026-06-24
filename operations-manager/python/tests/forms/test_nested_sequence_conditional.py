"""Regression test: per-item show_when conditionals work inside a NESTED sequence.

The bridge/renderer must resolve BOTH wildcards for a child of a nested sequence:
the parent (outer) [*] from the parent index and the child (inner) [*] from the
item index. This mirrors what `_build_nested_sequence` does before calling
`should_render_editable`. Guards the attachments use-coupling (provide-as ->
path vs env-name) and any future nested conditional fields.
"""

import copy

from opi.forms.editables.path import resolve_path
from opi.forms.visualizers.bridge import should_render_editable
from opi.forms.visualizers.fields.components import ATTACHMENT_USE_ENV_NAME, ATTACHMENT_USE_PATH

YAML_DATA = {
    "components": [
        {
            "name": "api",
            "services": [
                {
                    "attachments": {
                        "use": [
                            {"reference": "a", "provide-as": "file", "path": "/etc/tls/a"},
                            {"reference": "b", "provide-as": "env-var", "env-name": "B"},
                        ]
                    }
                }
            ],
        }
    ]
}


def _renders(visualizer, parent_index: int, child_index: int) -> bool:
    """Replicate _build_nested_sequence: pre-resolve the parent [*] in path + depends_on,
    then evaluate show_when with the child index."""
    resolved = copy.copy(visualizer)
    ed = copy.copy(visualizer.editable)
    ed.yaml_path = resolve_path(visualizer.editable.yaml_path, parent_index)
    if ed.depends_on:
        ed.depends_on = resolve_path(ed.depends_on, parent_index)
    resolved.editable = ed
    return should_render_editable(resolved, YAML_DATA, index=child_index)


def test_path_visible_only_for_file_items() -> None:
    assert _renders(ATTACHMENT_USE_PATH, 0, 0) is True  # provide-as: file
    assert _renders(ATTACHMENT_USE_PATH, 0, 1) is False  # provide-as: env-var


def test_env_name_visible_only_for_env_var_items() -> None:
    assert _renders(ATTACHMENT_USE_ENV_NAME, 0, 0) is False  # provide-as: file
    assert _renders(ATTACHMENT_USE_ENV_NAME, 0, 1) is True  # provide-as: env-var
