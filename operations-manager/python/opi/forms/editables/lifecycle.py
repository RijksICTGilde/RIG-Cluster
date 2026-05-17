"""FormState machine and lifecycle hooks for Editable processing.

Wraps the EditableFormProcessor pipeline with a state machine that fires
hooks registered on Editable instances at each lifecycle stage. Based on
the TAD editable system's state machine pattern.

Usage::

    lifecycle = EditableLifecycle()
    await lifecycle.run_hooks(
        FormState.PRE_SAVE,
        editables=all_editables,
        yaml_data=final_data,
        context={"project_name": "my-project"},
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.forms.editables.editable import FormState  # noqa: TC001 - used at runtime for dict key lookup

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer

logger = logging.getLogger(__name__)


def collect_hooks(
    editables: list[EditableVisualizer],
    state: FormState,
) -> list[tuple[str, Any]]:
    """Collect all hooks for a given state from editables, recursing into children.

    Returns a list of (yaml_path, hook) tuples sorted by hook.order.
    """
    hooks: list[tuple[str, Any]] = []
    for vis in editables:
        ed = vis.editable
        if ed.hooks and state in ed.hooks:
            hooks.append((ed.yaml_path, ed.hooks[state]))
        if vis.children:
            hooks.extend(collect_hooks(vis.children, state))
    hooks.sort(key=lambda h: getattr(h[1], "order", 0))
    return hooks


async def run_hooks(
    state: FormState,
    editables: list[EditableVisualizer],
    yaml_data: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> None:
    """Run all hooks for a given state across all editables.

    Hooks are collected from all editables (including children),
    sorted by order, and executed sequentially. Each hook receives
    the yaml_data dict and may mutate it in place.

    Args:
        state: The FormState to run hooks for.
        editables: All editables in the flow.
        yaml_data: The YAML data being processed (mutable).
        context: Optional context dict (e.g. project_name, user).
    """
    ctx = context or {}
    hooks = collect_hooks(editables, state)
    for yaml_path, hook in hooks:
        try:
            await hook.execute(yaml_data, ctx)
        except Exception:
            logger.exception("Hook failed for %s at state %s", yaml_path, state)
            raise
