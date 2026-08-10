"""Wizard state management for multi-step form flows.

WizardState holds intermediate form data as the user navigates wizard steps.
WizardSteps provides structured navigation context for templates.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from opi.forms.editables.editable import SERVICE_VIRTUALIZE
from opi.forms.editables.merge import deep_merge_into
from opi.forms.wizard.services_merge import merge_service_lists, service_name
from opi.services.services import service_entry_body

#: The keys whose value is a services list: the selection and the virtual config key.
#: The same pair the service packages declare and ``service_path`` resolves.
_SERVICE_LIST_KEYS = SERVICE_VIRTUALIZE

CLEARED_FIELD = "__wizard-field-cleared__"
"""Tombstone marker for fields the user cleared in a wizard step.

Step fragments are merged additively over the template snapshot in
``get_merged_data``, so a key that is simply absent cannot delete the
snapshot's old value. ``_extract_section_data`` stores this marker for
owned-but-absent fields; ``get_merged_data`` strips marked keys after
merging.
"""


def _strip_cleared_fields(value: Any) -> None:
    """Recursively remove dict entries whose value is the CLEARED_FIELD marker."""
    if isinstance(value, dict):
        for key in [k for k, v in value.items() if v == CLEARED_FIELD]:
            del value[key]
        for child in value.values():
            _strip_cleared_fields(child)
    elif isinstance(value, list):
        for item in value:
            _strip_cleared_fields(item)


def _config_overlay(entry: Any, name: str | None) -> dict[str, Any] | None:
    """The config-bearing fields of a service entry, without its identity key.

    ``service_entry_body`` returns the entry itself for the record form, so its
    identity (``name``/``reference``) sits among the fields to overlay. Carrying that
    key into a merge grafts a stray ``name`` onto the target, so strip it here: the
    target already knows who it is.
    """
    body = service_entry_body(entry, name)
    if not isinstance(body, dict):
        return None
    if body is entry:
        return {key: value for key, value in body.items() if key not in ("name", "reference")}
    return body


def _fold_virtual(container: dict[str, Any], real_key: str, virt_data: Any) -> None:
    """Fold one virtual payload onto its real sibling inside *container*."""
    real_data = container.get(real_key)
    if isinstance(real_data, list) and isinstance(virt_data, (list, dict)):
        # The carrier arrives in two shapes: a list of entries, or a name -> body
        # mapping (what a single-service config section posts, e.g.
        # {"keycloak": {"config": {...}}}). Reduce both to name -> entry so the fold
        # below does not have to care which one it got.
        carrier_by_name: dict[str, Any] = {}
        if isinstance(virt_data, dict):
            carrier_by_name = {name: {name: body} for name, body in virt_data.items() if isinstance(body, dict)}
        else:
            for entry in virt_data:
                name = service_name(entry)
                if name is not None and isinstance(entry, dict):
                    carrier_by_name[name] = entry
        # Fold each carried config onto its selected entry. Only names already in the
        # selection are touched: a service the user deselected must not come back
        # from a stale carrier.
        #
        # Merging the config-bearing FIELDS rather than whole entries is what makes
        # this format-agnostic: ``service_entry_body`` hands back the live sub-dict for
        # the legacy form ({keycloak: {config}}) and the entry itself for the record
        # form ({name: keycloak, config}), so the same overlay lands correctly on
        # either. Merging whole entries grafted the carrier's wrapper key onto the
        # target whenever the two forms differed.
        #
        # Matching on identity rather than on the entry still being a bare string is
        # what makes a SECOND edit stick. Once config has been saved the entry is a
        # dict, and the old string-only check skipped it without a word: the modal
        # reported success, the store logged "no change", and the project kept its
        # previous value (toets-hn7 keycloak template, 2026-08-05).
        for i, entry in enumerate(real_data):
            name = service_name(entry)
            if name is None or name not in carrier_by_name:
                continue
            carrier = carrier_by_name[name]
            target_body = service_entry_body(entry, name)
            overlay = _config_overlay(carrier, name)
            if isinstance(target_body, dict) and overlay is not None:
                deep_merge_into(target_body, overlay)
            elif not isinstance(target_body, dict):
                # A bare selection entry has no body yet; take the carrier's own form.
                real_data[i] = copy.deepcopy(carrier)
    elif isinstance(virt_data, dict):
        if isinstance(real_data, dict):
            real_data.update(virt_data)
        else:
            container[real_key] = virt_data


def _update_item(target: dict[str, Any], src: dict[str, Any]) -> None:
    """Overlay one section's version of a list item onto the merged one.

    A plain ``update`` everywhere except the item's own service list: a section stores
    only the service entries it configures (see ``_extract_section_data``), so replacing
    the list would drop every other service's deployment config -- clone state, a
    cross-domain patch -- for a section that never mentioned them. Merged by name, the
    same rule the project-level services list already follows (RC-60).
    """
    import copy

    for key, value in src.items():
        if key in _SERVICE_LIST_KEYS and isinstance(target.get(key), list) and isinstance(value, list):
            target[key] = merge_service_lists(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _devirtualize(value: Any, virt_mappings: dict[str, str]) -> None:
    """Fold virtual keys onto their real siblings at every level, then drop them.

    A virtual key (e.g. ``_services-config``) is a form-transport concern: it
    exists so a config section does not collide with the selection list it
    configures. It must never reach project data.

    The fold walks the whole structure rather than only the root because
    ``services`` occurs both project-wide and per component. Popping at the root
    only left ``components[i]._services-config`` behind, which the schema rejects
    (``additionalProperties: false`` on ``component``). Components whose service
    editable happened to run were cleaned as a side effect during field
    processing; a component with no services at all never was.
    """
    if isinstance(value, list):
        for item in value:
            _devirtualize(item, virt_mappings)
        return
    if not isinstance(value, dict):
        return
    for virt_key, real_key in virt_mappings.items():
        if virt_key in value:
            _fold_virtual(value, real_key, value.pop(virt_key))
    for child in value.values():
        _devirtualize(child, virt_mappings)


@dataclass
class WizardSteps:
    """Structured navigation context for wizard templates.

    Provides all the information needed to render the step indicator
    and navigation buttons.
    """

    current: str
    """Current section_id."""

    all: list[str]
    """Ordered section_ids including conditional ones that are currently active."""

    titles: dict[str, str]
    """Mapping of section_id to display title."""

    icons: dict[str, str | None]
    """Mapping of section_id to icon name."""

    completed: list[str]
    """Section_ids that have been completed."""

    @property
    def count(self) -> int:
        """Total number of active steps."""
        return len(self.all)

    @property
    def index(self) -> int:
        """0-based index of the current step."""
        try:
            return self.all.index(self.current)
        except ValueError:
            return 0

    @property
    def first(self) -> str:
        """First step section_id."""
        return self.all[0]

    @property
    def last(self) -> str:
        """Last step section_id."""
        return self.all[-1]

    @property
    def prev(self) -> str | None:
        """Previous step section_id, or None if at the first step."""
        idx = self.index
        return self.all[idx - 1] if idx > 0 else None

    @property
    def next(self) -> str | None:
        """Next step section_id, or None if at the last step."""
        idx = self.index
        return self.all[idx + 1] if idx < len(self.all) - 1 else None

    @property
    def is_first(self) -> bool:
        """Whether the current step is the first step."""
        return self.current == self.first

    @property
    def is_last(self) -> bool:
        """Whether the current step is the last step."""
        return self.current == self.last

    @property
    def progress_pct(self) -> int:
        """Progress percentage (0-100) based on completed steps."""
        if self.count == 0:
            return 0
        return round(len(self.completed) / self.count * 100)


@dataclass
class WizardState:
    """Server-side state for a wizard form session.

    Stored in the Starlette session. Tracks which steps are completed,
    holds validated form data per step, and resolves the active step list
    (including conditional sections).

    A WIZARD IS A BASE PLUS MUTATIONS
    ---------------------------------
    Two things make up the result, and every flow uses both:

    - the **base** (``base_data``): what was already there before the user
      started. Empty-with-seeds for the create wizard (there is no project
      yet); the part of the project file this flow does not own for an edit
      flow. The base is never written by a form; it is the floor the result
      stands on.
    - the **mutations** (``step_data``, one entry per section): what the user
      changed. A section stores only the fields its own editables own.

    ``get_merged_data`` is base plus mutations, in that order. The create and
    edit flows differ in what the base *is*, and in nothing else.

    THE RULE FOR "ABSENT"
    ---------------------
    A key that is not in a mutation means *unchanged*, never *removed*. A form
    posts what it renders, and what it does not render (a collapsed section, a
    step the user never opened, a locked field) is simply missing -- treating
    that as a removal is how services disappeared from project files.

    Removal is therefore always explicit:

    - a field the user emptied is stored as ``CLEARED_FIELD`` (a tombstone) and
      deleted after merging;
    - an item unticked in a SELECTION list is removed by
      ``apply_selection_mutation`` -- but only if the form actually offered it,
      because a name the form never showed cannot have been unticked.
    """

    flow_id: str
    """Which FormFlow this state belongs to."""

    current_step: str
    """Section_id of the currently displayed step."""

    completed_steps: list[str] = field(default_factory=list)
    """Section_ids that have been successfully validated."""

    step_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Validated form data per section_id."""

    active_sections: list[str] = field(default_factory=list)
    """Ordered section_ids including conditional ones currently visible."""

    project_name: str | None = None
    """None for create wizard, set for edit wizard."""

    base_version: str | None = None
    """Version of the project file this wizard was seeded from (ProjectStore token).

    Travels with the resulting write so it is applied as a change relative to what
    the user saw, and a change someone else made in the meantime is merged rather
    than overwritten. None for the create wizard: there is no earlier version.
    """

    base_data: dict[str, Any] = field(default_factory=dict)
    """The base: what was already there before this wizard started.

    Lowest-priority layer of ``get_merged_data``; every mutation in
    ``step_data`` is applied on top of it.

    - create wizard: the project template plus the seeds the first steps need
      (there is no project yet, so the base is a skeleton);
    - edit flows: the part of the project file this flow does NOT own. Keys a
      flow does own are deliberately left out -- see ``_fully_owned_list_keys``
      in ``router_detail_edit`` -- because a base copy of a list would resurrect
      items the user removed.

    Also carries render-only context that is not project data (``is_new``,
    ``existing_component_names``, ``_backup_runs``). Those never reach a project
    file because a save writes only the yaml_paths the flow's editables declare
    (see ``wizard/write_set.py``), not everything the session happens to hold.
    """

    locked_services: list[str] = field(default_factory=list)
    """Services that existed in the project before the wizard started.
    These cannot be removed and are rendered as locked in the service cards."""

    stashed_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Step data parked when a conditional section becomes inactive.

    Keyed by section_id, same structure as step_data.  When the section
    becomes active again the data is restored so previously entered
    config is not lost.
    """

    virt_mappings: dict[str, str] = field(default_factory=dict)
    """Virtual-to-real key mappings for virtualized editables.

    Populated from editable ``virtualize`` metadata during flow init.
    Maps virtual key (e.g. ``_services-config``) to real key (e.g.
    ``services``).  Used by ``get_merged_data`` to fold virtual data
    back into the real structure.
    """

    staged_attachments: dict[str, Any] = field(default_factory=dict)
    """Attachment uploads staged this session (id -> {filename, content="staging:<token>"}).

    Kept OUT of ``step_data`` on purpose: that dict is keyed by section_id and
    ``stash_inactive_sections`` would treat a stray non-section key as an inactive
    section and discard it on the next step. Combined into the project at submit
    (create: AttachmentStagingResolveGenerator; edit: ResolveAttachmentsHook).
    """

    @property
    def is_edit(self) -> bool:
        """Whether this wizard has an existing project as its base.

        The one question behind every create-vs-edit difference: does a project
        already exist? Read it here rather than re-deriving
        ``project_name is not None`` at each call site -- that derivation was
        repeated ten times in the wizard router alone, which is how a rule can
        hold in one flow and not in the other.
        """
        return self.project_name is not None

    def get_merged_data(self, strip_cleared: bool = True) -> dict[str, Any]:
        """Merge the base and the mutations into a single dict.

        Merge order (later overrides earlier):
        1. base_data (what was already there)
        2. step_data per active section (the user's mutations)
        3. devirtualize: fold virtual keys back into real keys

        For list values (e.g. ``deployments``), items are merged by index
        so that sections sharing a top-level key combine their fields
        instead of overwriting each other.

        When *strip_cleared* is True (the default), ``CLEARED_FIELD``
        tombstones are removed after merging. Callers that perform a
        further ``dict.update``-style merge into stored project data
        (which cannot express a deleted key) pass ``strip_cleared=False``
        so the tombstones survive to that merge and can be honored there.
        """
        import copy

        merged: dict[str, Any] = {}
        if self.base_data:
            merged.update(copy.deepcopy(self.base_data))
        for section_id in self.active_sections:
            if section_id not in self.step_data:
                continue
            for key, value in self.step_data[section_id].items():
                if key in _SERVICE_LIST_KEYS and isinstance(merged.get(key), list) and isinstance(value, list):
                    # Services is a selection set keyed by service name, not positional.
                    # Merge by name so a section still carrying the pre-edit list cannot
                    # index-swap or duplicate services (see services_merge).
                    #
                    # The same holds for the virtual key that carries the CONFIG, and that
                    # was missing: each step stores only the services it configures, so a
                    # plain replace made the keycloak step's config vanish behind the
                    # invite step's. It stayed hidden while every step still carried a copy
                    # of everything -- then the last copy won, stale value and all, which is
                    # what made the keycloak template reappear with its old value.
                    merged[key] = merge_service_lists(merged[key], value)
                elif key in merged and isinstance(merged[key], list) and isinstance(value, list):
                    # Selection lists (all-scalar) replace entirely. Structural lists
                    # (contain dicts, e.g. deployments) merge by index so that sections
                    # sharing a key combine their fields.
                    if all(not isinstance(item, dict) for item in value):
                        merged[key] = copy.deepcopy(value)
                    else:
                        target = merged[key]
                        for i, src_item in enumerate(value):
                            if i < len(target):
                                if isinstance(target[i], dict) and isinstance(src_item, dict):
                                    _update_item(target[i], src_item)
                                else:
                                    target[i] = copy.deepcopy(src_item)
                            else:
                                target.append(copy.deepcopy(src_item))
                elif key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                    # Merge dicts (e.g. virtual keys from multiple config sections)
                    merged[key].update(copy.deepcopy(value))
                else:
                    merged[key] = copy.deepcopy(value)

        # Devirtualize: fold virtual keys back into real keys so that
        # smart_get_value can find config at real yaml_paths.
        _devirtualize(merged, self.virt_mappings)

        if strip_cleared:
            _strip_cleared_fields(merged)
        return merged

    def populate_virt_mappings(self, sections: list[Any]) -> None:
        """Extract virtualize mappings from flow sections.

        Scans all editables in the given sections and records any
        ``virtualize`` tuples as virtual-to-real key mappings.
        """
        for section in sections:
            for vis in getattr(section, "editables", []):
                virt = getattr(vis.editable, "virtualize", None)
                if virt:
                    self.virt_mappings[virt[1]] = virt[0]

    def get_steps(
        self,
        sections: dict[str, tuple[str, str | None]],
    ) -> WizardSteps:
        """Build a WizardSteps navigation context.

        Args:
            sections: Mapping of section_id to (title, icon) for all
                      active sections.
        """
        return WizardSteps(
            current=self.current_step,
            all=list(self.active_sections),
            titles={sid: sections[sid][0] for sid in self.active_sections if sid in sections},
            icons={sid: sections[sid][1] for sid in self.active_sections if sid in sections},
            completed=list(self.completed_steps),
        )

    def mark_completed(self, section_id: str) -> None:
        """Mark a section as completed."""
        if section_id not in self.completed_steps:
            self.completed_steps.append(section_id)

    def store_step_data(self, section_id: str, data: dict[str, Any]) -> None:
        """Store validated form data for a section."""
        self.step_data[section_id] = data

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for session storage."""
        return {
            "flow_id": self.flow_id,
            "current_step": self.current_step,
            "completed_steps": self.completed_steps,
            "step_data": self.step_data,
            "active_sections": self.active_sections,
            "project_name": self.project_name,
            "base_version": self.base_version,
            "base_data": self.base_data,
            "stashed_data": self.stashed_data,
            "locked_services": self.locked_services,
            "virt_mappings": self.virt_mappings,
            "staged_attachments": self.staged_attachments,
        }

    def stash_inactive_sections(self, active_section_ids: list[str]) -> None:
        """Move step_data for newly-inactive sections to stashed_data.

        Also removes them from completed_steps.  When a section becomes
        active again, its data is restored from the stash so the user
        doesn't lose previously entered configuration.
        """
        active_set = set(active_section_ids)

        # Stash data for sections that are no longer active
        for sid in list(self.step_data):
            if sid not in active_set:
                self.stashed_data[sid] = self.step_data.pop(sid)
                if sid in self.completed_steps:
                    self.completed_steps.remove(sid)

        # Restore data for sections that became active again
        for sid in active_section_ids:
            if sid not in self.step_data and sid in self.stashed_data:
                self.step_data[sid] = self.stashed_data.pop(sid)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WizardState:
        """Deserialize from a session dict."""
        return cls(
            flow_id=data["flow_id"],
            current_step=data["current_step"],
            completed_steps=data.get("completed_steps", []),
            step_data=data.get("step_data", {}),
            active_sections=data.get("active_sections", []),
            project_name=data.get("project_name"),
            base_version=data.get("base_version"),
            # ``template_data`` is the old name for the same layer; a session written
            # before the rename must keep working across a deploy.
            base_data=data.get("base_data", data.get("template_data", {})),
            stashed_data=data.get("stashed_data", {}),
            locked_services=data.get("locked_services", []),
            virt_mappings=data.get("virt_mappings", {}),
            staged_attachments=data.get("staged_attachments", {}),
        )
