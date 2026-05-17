"""Reindex editable paths - resolve [0] placeholders to concrete [N] indices.

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

    Unlike ``materialize_wildcard_editable`` (which replaces the *first* ``[*]``),
    this targets a specific substring - e.g. ``"deployments[*]"`` →
    ``"deployments[0]"`` - leaving other wildcards like ``components[*]``
    intact.
    """

    def _replace(s: str | None) -> str | None:
        return s.replace(old_segment, new_segment) if s else s

    children = (
        [replace_segment_editable(c, old_segment, new_segment) for c in ed.children] if ed.children else ed.children
    )

    return dataclasses.replace(
        ed,
        yaml_path=_replace(ed.yaml_path) or ed.yaml_path,
        depends_on=_replace(ed.depends_on),
        defers_to=_replace(ed.defers_to),
        children=children,
    )


def replace_segment_visualizer(vis: EditableVisualizer, old_segment: str, new_segment: str) -> EditableVisualizer:
    """Replace a specific path segment in a visualizer's editable and children."""
    children = (
        [replace_segment_visualizer(c, old_segment, new_segment) for c in vis.children]
        if vis.children
        else vis.children
    )

    return dataclasses.replace(
        vis,
        editable=replace_segment_editable(vis.editable, old_segment, new_segment),
        children=children,
    )


def materialize_wildcard_editable(ed: Editable, index: int) -> Editable:
    """Replace the first ``[*]`` with ``[index]`` in editable paths.

    Used when extracting a single item from a sequence for standalone editing.
    Only the first wildcard is replaced so nested sequences (e.g. ports within
    a component) keep their ``[*]`` for the sequence renderer to iterate.
    """
    target = f"[{index}]"

    def _replace(s: str | None) -> str | None:
        return s.replace("[*]", target, 1) if s else s

    children = [materialize_wildcard_editable(c, index) for c in ed.children] if ed.children else ed.children

    # Clone generator with updated deployment_index if it has one
    generator = ed.generator
    if generator and hasattr(generator, "deployment_index"):
        import copy

        generator = copy.copy(generator)
        generator.deployment_index = index  # type: ignore[attr-defined]

    # Clone show_when condition with updated deployment_index if it has one
    show_when = ed.show_when
    if show_when and hasattr(show_when, "deployment_index"):
        import copy

        show_when = copy.copy(show_when)
        show_when.deployment_index = index  # type: ignore[attr-defined]

    # Clone enforcer with updated deployment_index if it has one — without this,
    # group-level enforcers (e.g. DomainConfigEnforcer) keep their default
    # deployment_index=0 and validate the wrong deployment when editing
    # deployments[1+].
    enforcer = ed.enforcer
    if enforcer and hasattr(enforcer, "deployment_index"):
        import copy

        enforcer = copy.copy(enforcer)
        enforcer.deployment_index = index  # type: ignore[attr-defined]

    return dataclasses.replace(
        ed,
        yaml_path=_replace(ed.yaml_path) or ed.yaml_path,
        depends_on=_replace(ed.depends_on),
        defers_to=_replace(ed.defers_to),
        children=children,
        generator=generator,
        show_when=show_when,
        enforcer=enforcer,
    )


def materialize_wildcard_visualizer(vis: EditableVisualizer, index: int) -> EditableVisualizer:
    """Replace ``[*]`` with ``[index]`` in a visualizer's editable and children."""
    children = [materialize_wildcard_visualizer(c, index) for c in vis.children] if vis.children else vis.children

    return dataclasses.replace(
        vis,
        editable=materialize_wildcard_editable(vis.editable, index),
        children=children,
    )


def materialize_wildcard_layout(layout: list[Any], index: int) -> list[Any]:
    """Replace ``[*]`` with ``[index]`` in layout path strings and DisplayBlock context.

    Counterpart to ``materialize_wildcard_visualizer`` for layout lists.
    """
    from opi.forms.layout import DisplayBlock

    target = f"[{index}]"
    result: list[Any] = []
    for item in layout:
        if isinstance(item, str):
            result.append(item.replace("[*]", target, 1))
        elif isinstance(item, DisplayBlock) and "deployment_index" in item.context:
            result.append(dataclasses.replace(item, context={**item.context, "deployment_index": index}))
        else:
            result.append(item)
    return result


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

    String paths have ``[from_index]`` replaced with ``[to_index]``.
    DisplayBlock elements with a ``deployment_index`` in their context
    are cloned with the updated index so that their compute function
    reads the correct deployment.
    """
    if from_index == to_index:
        return layout

    from opi.forms.layout import DisplayBlock

    old = f"[{from_index}]"
    new = f"[{to_index}]"
    result: list[Any] = []
    for item in layout:
        if isinstance(item, str):
            result.append(item.replace(old, new))
        elif isinstance(item, DisplayBlock) and "deployment_index" in item.context:
            result.append(dataclasses.replace(item, context={**item.context, "deployment_index": to_index}))
        else:
            result.append(item)
    return result
