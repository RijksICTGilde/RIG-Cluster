"""Wizard state management for multi-step form flows.

WizardState holds intermediate form data as the user navigates wizard steps.
WizardSteps provides structured navigation context for templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opi.forms.wizard.services_merge import merge_service_lists, service_name

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


def _fold_virtual(container: dict[str, Any], real_key: str, virt_data: Any) -> None:
    """Fold one virtual payload onto its real sibling inside *container*."""
    real_data = container.get(real_key)
    if isinstance(virt_data, list) and isinstance(real_data, list):
        # Build lookup from virtual list entries, keyed by service identity so
        # BOTH entry forms resolve: the uniform record ({name, config}) and the
        # legacy single-key dict ({keycloak: {config}}). Keying on the raw dict
        # keys understood only the legacy form, so a record's config was dropped
        # here and every keycloak/db config edit silently vanished on merge.
        config_by_name: dict[str, dict[str, Any]] = {}
        for entry in virt_data:
            if isinstance(entry, dict):
                name = service_name(entry)
                if name is not None:
                    config_by_name[name] = entry
        # Fold each carried config onto its selected entry. Only names already in
        # the selection are touched: a service the user deselected must not come
        # back from a stale carrier.
        for i, entry in enumerate(real_data):
            name = service_name(entry)
            if name is not None and name in config_by_name:
                real_data[i] = merge_service_lists([entry], [config_by_name[name]])[0]
    elif isinstance(virt_data, dict) and isinstance(real_data, list):
        # Virtual data is a dict (name -> config), real data is a
        # mixed list.  Replace matching plain-string entries with
        # their config dict equivalents.
        for i, entry in enumerate(real_data):
            if isinstance(entry, str) and entry in virt_data:
                real_data[i] = {entry: virt_data[entry]}
    elif isinstance(virt_data, dict):
        if isinstance(real_data, dict):
            real_data.update(virt_data)
        else:
            container[real_key] = virt_data


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

    template_data: dict[str, Any] = field(default_factory=dict)
    """Static project template data (repositories, etc.) - lowest priority layer."""

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

    def get_merged_data(self, strip_cleared: bool = True) -> dict[str, Any]:
        """Merge template and step data into a single dict.

        Merge order (later overrides earlier):
        1. template_data (static skeleton - repositories, base config)
        2. step_data per active section (user-entered form values)
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
        if self.template_data:
            merged.update(copy.deepcopy(self.template_data))
        for section_id in self.active_sections:
            if section_id not in self.step_data:
                continue
            for key, value in self.step_data[section_id].items():
                if key == "services" and isinstance(merged.get(key), list) and isinstance(value, list):
                    # Services is a selection set keyed by service name, not positional.
                    # Merge by name so a section still carrying the pre-edit list cannot
                    # index-swap or duplicate services (see services_merge).
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
                                    target[i].update(copy.deepcopy(src_item))
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
            "template_data": self.template_data,
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
            template_data=data.get("template_data", {}),
            stashed_data=data.get("stashed_data", {}),
            locked_services=data.get("locked_services", []),
            virt_mappings=data.get("virt_mappings", {}),
            staged_attachments=data.get("staged_attachments", {}),
        )
