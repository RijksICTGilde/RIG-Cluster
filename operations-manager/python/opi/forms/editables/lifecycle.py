"""FormState machine and lifecycle hooks for Editable processing.

Wraps the EditableFormProcessor pipeline with a state machine that fires
hooks registered on Editable instances at each lifecycle stage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable

logger = logging.getLogger(__name__)


class FormState(StrEnum):
    """Lifecycle stages for form processing."""

    VALIDATE = "validate"
    CONVERT = "convert"
    PRE_SAVE = "pre_save"
    SAVE = "save"
    POST_SAVE = "post_save"


@runtime_checkable
class EditableHook(Protocol):
    """Hook that fires at a specific FormState."""

    def execute(self, context: HookContext) -> None:
        """Execute hook logic. May mutate context or set context.halt = True."""
        ...


@dataclass
class HookContext:
    """Mutable context passed to hooks during lifecycle processing."""

    yaml_data: dict[str, Any]
    form_data: dict[str, Any]
    editables: list[Editable]
    errors: list[str] = field(default_factory=list)
    halt: bool = False


@dataclass
class ProcessResult:
    """Result of a lifecycle processing run."""

    yaml_data: dict[str, Any]
    errors: dict[str, list[str]] = field(default_factory=dict)
    global_errors: list[str] = field(default_factory=list)
    halted_at: FormState | None = None

    @property
    def success(self) -> bool:
        return not self.errors and not self.global_errors and self.halted_at is None


class EditableLifecycle:
    """State machine wrapping the processor pipeline.

    Processes form submissions through a defined sequence of states,
    firing hooks registered on each Editable at the appropriate stage.

    Usage::

        lifecycle = EditableLifecycle(processor)
        result = lifecycle.process(
            form_data=form_data,
            yaml_data=yaml_data,
            editables=editables,
            is_edit=False,
        )
        if result.success:
            # result.yaml_data contains the processed output
            ...
    """

    STATES = (
        FormState.VALIDATE,
        FormState.CONVERT,
        FormState.PRE_SAVE,
        FormState.SAVE,
        FormState.POST_SAVE,
    )

    def __init__(self, processor: Any) -> None:
        """Initialize with an EditableFormProcessor instance."""
        self._processor = processor

    def process(
        self,
        form_data: dict[str, Any],
        yaml_data: dict[str, Any],
        editables: list[Editable],
        *,
        is_edit: bool = False,
    ) -> ProcessResult:
        """Run the full lifecycle: VALIDATE -> CONVERT -> PRE_SAVE -> SAVE -> POST_SAVE.

        At each state, runs the processor's corresponding step, then fires
        any hooks registered on the editables for that state.

        Args:
            form_data: Submitted form data (flat or nested).
            yaml_data: Current YAML project data.
            editables: The Editable instances to process.
            is_edit: Whether this is an edit (vs create) operation.

        Returns:
            ProcessResult with the final yaml_data and any errors.
        """
        result = ProcessResult(yaml_data=yaml_data)
        hook_context = HookContext(
            yaml_data=yaml_data,
            form_data=form_data,
            editables=editables,
        )

        for state in self.STATES:
            # Fire hooks for this state
            self._fire_hooks(state, editables, hook_context)

            if hook_context.halt:
                result.halted_at = state
                result.global_errors.extend(hook_context.errors)
                logger.info("Lifecycle halted at state %s", state)
                break

            if hook_context.errors:
                result.global_errors.extend(hook_context.errors)
                hook_context.errors.clear()

        # Sync back any mutations from hooks
        result.yaml_data = hook_context.yaml_data
        return result

    def _fire_hooks(
        self,
        state: FormState,
        editables: list[Editable],
        context: HookContext,
    ) -> None:
        """Fire all hooks registered for the given state."""
        for editable in editables:
            if not editable.hooks:
                continue
            hook = editable.hooks.get(state)
            if hook is None:
                continue
            try:
                hook.execute(context)
            except Exception:
                logger.exception(
                    "Hook failed for editable %s at state %s",
                    editable.yaml_path,
                    state,
                )
                context.errors.append(f"Hook error for {editable.yaml_path} at {state}")
