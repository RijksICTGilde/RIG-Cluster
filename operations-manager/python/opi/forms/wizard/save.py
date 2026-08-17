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

from fastapi import HTTPException

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
from opi.forms.wizard.secrets import restore_redacted_secrets
from opi.forms.wizard.state import _strip_cleared_fields
from opi.forms.wizard.write_set import apply_write_paths, flow_write_paths
from opi.services.schema_migration import normalize_service_entries
from opi.web.project_edit_security import IMMUTABLE_PROJECT_FIELDS

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)


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


def guard_immutable_paths(write_paths: list[str]) -> None:
    """Refuse to save when a flow declares a field no form may write.

    ``name`` is the on-disk filename and ``clusters`` is not editable yet. A
    flow declaring either is a bug in the flow, not something a user did, so
    fail loud rather than silently dropping the value.
    """
    offending = sorted({path for path in write_paths if path.split("/")[0].split("[")[0] in IMMUTABLE_PROJECT_FIELDS})
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"Flow declares immutable project fields: {', '.join(offending)}",
        )


def _system_fields_for_new_deployment(existing_data: dict[str, Any], project_name: str) -> None:
    """Fill in the fields a new deployment needs but no form collects.

    Copies cluster/repository from an existing deployment, falls back to the project's
    own declaration, and defaults the namespace to the project name.

    That fallback is the FIRST deployment of a project. Copying from a sibling works
    from the second one on, and every project used to be born with one, so the gap did
    not show. A project created through the API has no deployments at all, and the
    deployment added to it afterwards then carried neither cluster nor repository -
    which surfaced much later as "Repository configuration not found: None" when the
    project was processed, nowhere near the save that caused it.

    Only when the project leaves no choice. With several repositories or clusters,
    picking one here would be a guess; validation refuses the file instead, and that
    says more than an arbitrary pick.
    """
    deployments = existing_data.get("deployments", [])
    if not deployments or not isinstance(deployments[-1], dict):
        return
    new_dep = deployments[-1]
    existing_dep = next((d for d in deployments[:-1] if isinstance(d, dict)), None)
    new_dep.setdefault("namespace", project_name)

    for field_name in ("cluster", "repository"):
        if field_name in new_dep:
            continue
        if existing_dep and field_name in existing_dep:
            new_dep[field_name] = existing_dep[field_name]
            continue
        fallback = _only_project_choice(existing_data, field_name)
        if fallback is not None:
            new_dep[field_name] = fallback


def _only_project_choice(project_data: dict[str, Any], field_name: str) -> Any | None:
    """The project's single cluster or repository, or None when it is not a single one.

    ``clusters`` is a list of names; ``repositories`` a list of blocks with a ``name``.
    """
    key = "clusters" if field_name == "cluster" else "repositories"
    entries = project_data.get(key) or []
    if len(entries) != 1:
        return None
    entry = entries[0]
    return entry.get("name") if isinstance(entry, dict) else entry


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

    # The restore below pairs the two structures key by key, so both sides have to spell a
    # service entry the same way. The editables write the legacy name-as-key shape
    # ({keycloak: {config: ...}}) while the stored project carries the uniform record
    # ({name: keycloak, config: ...}); walked against each other, nothing under a service
    # config lines up and every redacted secret there is dropped instead of restored -- the
    # realm admin password among them. Normalizing here (the same idempotent normalizer that
    # already runs at the end of this function) puts both sides in one shape first.
    normalize_service_entries(merged_data)

    # Put back the encrypted values the session was not allowed to carry, from the project
    # as just read from git. The write-path merge below already leaves them alone (nothing
    # names them, so nothing writes them), but the new-list-item branch right after this
    # appends an item *whole*, outside that discipline. Restoring here means a redaction
    # placeholder cannot reach the project file by any route.
    merged_data = restore_redacted_secrets(merged_data, existing_data)

    # A new list item has no stored version to protect, so it is appended
    # whole: the empty slot the wizard was seeded with carries fields no form
    # collects (the cluster of a new deployment) and they must come along.
    target = flow.target
    if target is not None and target.is_new:
        apply_list_item_merge(existing_data, merged_data, target.list_key, target.index, target.is_new)
        merged_data.pop(target.list_key, None)

        if target.list_key == "deployments":
            _system_fields_for_new_deployment(existing_data, project_name)

    # Everything else: write only what this flow's editables declare. What no
    # editable names is not written, and therefore does not need saving from
    # being overwritten either - that is what the template-only strip, and the
    # four separate leak fixes before it, were working around.
    write_paths = flow_write_paths(list(active_sections))
    guard_immutable_paths(write_paths)
    existing_data = apply_write_paths(existing_data, merged_data, write_paths)

    # Drop tombstones before anything downstream reads the data: a PRE_SAVE
    # hook seeing the marker sitting in a field would take it for a value.
    _strip_cleared_fields(existing_data)

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

    # The editables write service config in the legacy name-as-key shape
    # ({cross-domain-access: {config: ...}}), which the schema's component and deployment
    # service envelopes reject. The create wizard already normalizes at save
    # (``router_wizard.submit_wizard``); the modal-edit path did not, which is invisible
    # until a form writes a service block through this route -- the per-deployment
    # cross-domain patch is the first that does. Same normalizer, one canonical shape.
    normalize_service_entries(existing_data)

    # Ensure AGE-encrypted multiline values use literal block scalars
    _apply_literal_scalars(existing_data)

    # Defensive: drop any CLEARED_FIELD tombstones that survived the merges
    # above (e.g. via the top-level apply_form_data_to_project path) so they
    # never reach the saved project file.
    _strip_cleared_fields(existing_data)

    return existing_data
