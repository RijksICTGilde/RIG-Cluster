"""The one way a wizard flow's form data lands in the project file.

``apply_modal_edit`` is the whole journey from "the user pressed save" to
"this is the dict that goes to disk": merge the flow's data into the stored
project, run the section hooks, compute derived values, strip what must not
be persisted. It is deliberately free of I/O so the router owns reading from
git and writing back, and so the merge itself can be tested on plain dicts.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from opi.forms.editables.editable import Editable, FormState, WidgetType
from opi.forms.editables.hooks import (
    PreserveAttachmentContentHook,
    ResolveAttachmentsHook,
    StripTransientsHook,
)
from opi.forms.editables.lifecycle import run_hooks
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.resolvers import build_resolver_map
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.wizard.state import _strip_cleared_fields
from opi.web.project_edit_security import apply_form_data_to_project

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)


def detect_list_target(flow_id: str, state: Any) -> tuple[str, int, bool] | None:
    """Detect if a flow targets a single item in a list.

    Returns (list_key, index, is_new) or None for non-list flows.
    """
    for prefix, list_key in [
        ("modal-edit-component-", "components"),
        ("modal-edit-deployment-", "deployments"),
        ("modal-add-deployment-", "deployments"),
        ("modal-edit-domain-", "deployments"),
        ("modal-edit-backup-schedule-", "deployments"),
    ]:
        if flow_id.startswith(prefix):
            suffix = flow_id.removeprefix(prefix)
            if suffix.isdigit():
                idx = int(suffix)
                is_new = prefix == "modal-add-deployment-" or (
                    prefix == "modal-edit-component-" and state and (state.template_data or {}).get("is_new", False)
                )
                return list_key, idx, is_new
    return None


def apply_list_item_merge(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    list_key: str,
    idx: int,
    is_new: bool,
) -> None:
    """Merge a single list item into the existing project data.

    For add: appends the new item to the list.
    For edit: updates the item at the given index in-place, preserving
    fields the form didn't touch (e.g. readonly ``name``).

    A plain ``dict.update`` cannot express a deleted key, so a field the
    user cleared (dropped from *item_data*) would otherwise be resurrected
    from the existing item. ``item_data`` therefore carries ``CLEARED_FIELD``
    tombstones for such fields (the caller builds *merged_data* with
    ``strip_cleared=False``); after merging we drop the tombstoned keys.
    """
    source_list = merged_data.get(list_key)
    if not isinstance(source_list, list) or idx >= len(source_list):
        return

    item_data = copy.deepcopy(source_list[idx])
    existing_list = existing_data.setdefault(list_key, [])

    if is_new:
        _strip_cleared_fields(item_data)
        existing_list.append(item_data)
    elif idx < len(existing_list) and isinstance(existing_list[idx], dict):
        existing_list[idx].update(item_data)
        _strip_cleared_fields(existing_list[idx])
    elif idx < len(existing_list):
        _strip_cleared_fields(item_data)
        existing_list[idx] = item_data


def template_only_keys(
    step_data: dict[str, dict[str, Any]],
    template_data: dict[str, Any] | None,
    virt_mappings: dict[str, str],
) -> set[str]:
    """Top-level keys present only as template context, to strip before the merged data
    overwrites the stored project.

    ``template_data`` carries context the step data does not (config for AGE decryption,
    existing names for uniqueness). Anything the step actually produced must NOT be treated
    as template-only, or the edit is reverted to the git baseline.

    Crucially, a section that produces a VIRTUAL key (e.g. ``_services-config``) owns the
    REAL key it folds into (``services``): ``get_merged_data`` has already devirtualized the
    edit into ``services``. Counting only the raw produced keys left ``services`` looking
    template-only, so every project-level service-config modal edit (a new invite, a keycloak
    template change) was popped and lost. ``virt_mappings`` maps virtual -> real, so we add
    each produced virtual key's real target to the produced set.
    """
    produced = {k for sd in step_data.values() for k in sd}
    produced |= {virt_mappings[k] for k in produced if k in virt_mappings}
    return set(template_data or {}) - produced


def _system_fields_for_new_deployment(existing_data: dict[str, Any], project_name: str) -> None:
    """Fill in the fields a new deployment needs but no form collects.

    Copies cluster/repository from an existing deployment and defaults the
    namespace to the project name.
    """
    deployments = existing_data.get("deployments", [])
    if not deployments or not isinstance(deployments[-1], dict):
        return
    new_dep = deployments[-1]
    existing_dep = next((d for d in deployments[:-1] if isinstance(d, dict)), None)
    new_dep.setdefault("namespace", project_name)
    if existing_dep:
        for field_name in ("cluster", "repository"):
            if field_name in existing_dep and field_name not in new_dep:
                new_dep[field_name] = existing_dep[field_name]


def _hook_editables(all_editables: list[Any]) -> list[EditableVisualizer]:
    """The three system hooks every modal save runs, as editables.

    They are editables so ``run_hooks`` can treat them like any other: strip
    transients, resolve files staged this session into the encrypted catalog
    (the project AGE key is present by now), and re-attach the attachment
    content the wizard session stripped.
    """
    return [
        EditableVisualizer(
            editable=Editable(
                yaml_path="_system/resolve-attachments",
                hooks={FormState.PRE_SAVE: ResolveAttachmentsHook()},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        ),
        EditableVisualizer(
            editable=Editable(
                yaml_path="_system/preserve-attachment-content",
                hooks={FormState.PRE_SAVE: PreserveAttachmentContentHook()},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        ),
        EditableVisualizer(
            editable=Editable(
                yaml_path="_system/strip-transients",
                hooks={FormState.PRE_SAVE: StripTransientsHook(all_editables)},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        ),
    ]


async def apply_modal_edit(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    *,
    flow: FormFlow,
    active_sections: list[FormSection],
    state: WizardState,
    project_name: str,
    original_attachment_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge one modal-wizard edit into *existing_data* and return the result.

    *merged_data* is the wizard's merged view built with ``strip_cleared=False``
    so cleared fields still carry their tombstone into this merge; the
    tombstones are removed here, right before the data is handed back for
    saving.
    """
    from opi.web.router_wizard import _apply_literal_scalars

    # Strip template-only keys: template_data provides context for rendering
    # and validation (e.g. config for AGE decryption, existing_deployment_names
    # for uniqueness checks) but should not overwrite existing project data.
    # JSON session round-trip also strips ruamel.yaml types (LiteralScalarString).
    for key in template_only_keys(state.step_data, state.template_data, state.virt_mappings):
        merged_data.pop(key, None)

    # Targeted list merge for flows that operate on a single list item.
    # Instead of replacing the entire list, we add or update one entry.
    list_target = detect_list_target(flow.flow_id, state)
    if list_target:
        list_key, idx, is_new = list_target
        apply_list_item_merge(existing_data, merged_data, list_key, idx, is_new)
        merged_data.pop(list_key, None)

        if list_key == "deployments" and is_new:
            _system_fields_for_new_deployment(existing_data, project_name)

    existing_data = apply_form_data_to_project(existing_data, merged_data)

    # Run post_merge hooks (e.g. distribute component refs to deployments)
    for section in active_sections:
        if section.post_merge:
            section.post_merge(existing_data, merged_data)

    # Compute derived values (e.g. issuer from base-domain)
    processor = EditableFormProcessor()
    for section in active_sections:
        processor.apply_dependent_generators(section.editables, existing_data)

    # PRE_SAVE hooks: run while transients are still available so that hooks
    # such as SubdomainRequestHook can read ``_request-subdomain`` and append
    # the corresponding entry to ``domains.allowed-subdomains``. Mirrors the
    # equivalent block in router_wizard.py.
    all_editables = [ed for section in active_sections for ed in section.editables]
    hook_context = {
        "project_name": project_name,
        "resolvers": build_resolver_map(all_editables),
        "staged_attachments": state.staged_attachments or {},
        "original_attachment_content": original_attachment_content or {},
    }
    await run_hooks(
        FormState.PRE_SAVE,
        [*all_editables, *_hook_editables(all_editables)],
        existing_data,
        hook_context,
    )

    # Defensive: ensure any transients not stripped by the PRE_SAVE chain
    # are still removed before save.
    for section in active_sections:
        processor.strip_transients_from(existing_data, section.editables)

    # Ensure AGE-encrypted multiline values use literal block scalars
    _apply_literal_scalars(existing_data)

    # Defensive: drop any CLEARED_FIELD tombstones that survived the merges
    # above (e.g. via the top-level apply_form_data_to_project path) so they
    # never reach the saved project file.
    _strip_cleared_fields(existing_data)

    return existing_data
