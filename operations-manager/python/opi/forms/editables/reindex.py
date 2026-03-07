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


def replace_segment_editable(ed: Editable, old_segment: str, new_segment: str) -> Editable:
    """Replace a specific path segment in all editable paths.

    Unlike ``materialize_wildcard_editable`` (which replaces *every* ``[*]``),
    this targets a specific substring — e.g. ``"deployments[*]"`` →
    ``"deployments[0]"`` — leaving other wildcards like ``components[*]``
    intact.
    """

    def _replace(s: str | None) -> str | None:
        return s.replace(old_segment, new_segment) if s else s

    children = [replace_segment_editable(c, old_segment, new_segment) for c in ed.children] if ed.children else ed.children

    return dataclasses.replace(
        ed,
        yaml_path=_replace(ed.yaml_path) or ed.yaml_path,
        depends_on=_replace(ed.depends_on),
        defers_to=_replace(ed.defers_to),
        children=children,
    )


def replace_segment_visualizer(vis: EditableVisualizer, old_segment: str, new_segment: str) -> EditableVisualizer:
    """Replace a specific path segment in a visualizer's editable and children."""
    children = [replace_segment_visualizer(c, old_segment, new_segment) for c in vis.children] if vis.children else vis.children

    return dataclasses.replace(
        vis,
        editable=replace_segment_editable(vis.editable, old_segment, new_segment),
        children=children,
    )


def materialize_wildcard_editable(ed: Editable, index: int) -> Editable:
    """Replace ``[*]`` with ``[index]`` in all editable paths.

    Used when extracting a single item from a sequence for standalone editing.
    """
    target = f"[{index}]"

    def _replace(s: str | None) -> str | None:
        return s.replace("[*]", target) if s else s

    children = [materialize_wildcard_editable(c, index) for c in ed.children] if ed.children else ed.children

    return dataclasses.replace(
        ed,
        yaml_path=_replace(ed.yaml_path) or ed.yaml_path,
        depends_on=_replace(ed.depends_on),
        defers_to=_replace(ed.defers_to),
        children=children,
    )


def materialize_wildcard_visualizer(vis: EditableVisualizer, index: int) -> EditableVisualizer:
    """Replace ``[*]`` with ``[index]`` in a visualizer's editable and children."""
    children = [materialize_wildcard_visualizer(c, index) for c in vis.children] if vis.children else vis.children

    return dataclasses.replace(
        vis,
        editable=materialize_wildcard_editable(vis.editable, index),
        children=children,
    )


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
