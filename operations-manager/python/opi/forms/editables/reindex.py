"""Reindex editable paths — resolve [0] placeholders to concrete [N] indices.

Used when editing a specific sequence item (e.g. deployment 2's domain config)
from editables originally defined with a fixed [0] index.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.visualizer import EditableVisualizer


def reindex_editable(ed: Editable, from_index: int, to_index: int) -> Editable:
    """Clone an Editable with all path references reindexed.

    Replaces ``[from_index]`` with ``[to_index]`` in yaml_path, depends_on,
    and defers_to. Recurses into children.
    """
    if from_index == to_index:
        return ed

    old = f"[{from_index}]"
    new = f"[{to_index}]"

    def _replace(s: str | None) -> str | None:
        return s.replace(old, new) if s else s

    children = [reindex_editable(c, from_index, to_index) for c in ed.children] if ed.children else ed.children

    return dataclasses.replace(
        ed,
        yaml_path=_replace(ed.yaml_path) or ed.yaml_path,
        depends_on=_replace(ed.depends_on),
        defers_to=_replace(ed.defers_to),
        children=children,
    )


def reindex_visualizer(vis: EditableVisualizer, from_index: int, to_index: int) -> EditableVisualizer:
    """Clone an EditableVisualizer with reindexed editable and children."""
    if from_index == to_index:
        return vis

    children = [reindex_visualizer(c, from_index, to_index) for c in vis.children] if vis.children else vis.children

    return dataclasses.replace(
        vis,
        editable=reindex_editable(vis.editable, from_index, to_index),
        children=children,
    )


def reindex_layout(layout: list[Any], from_index: int, to_index: int) -> list[Any]:
    """Clone a layout list with string path references reindexed.

    Non-string layout elements (DisplayBlock, TemplatePartial, Fieldset, etc.)
    are kept as-is — they don't contain index-specific paths.
    """
    if from_index == to_index:
        return layout

    old = f"[{from_index}]"
    new = f"[{to_index}]"
    result: list[Any] = []
    for item in layout:
        if isinstance(item, str):
            result.append(item.replace(old, new))
        else:
            result.append(item)
    return result
