"""Wizard state management for multi-step form flows.

WizardState holds intermediate form data as the user navigates wizard steps.
WizardSteps provides structured navigation context for templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
                if key in merged and isinstance(merged[key], list) and isinstance(value, list):
                    # Selection lists (all-scalar, e.g. services) replace entirely.
                    # Structural lists (contain dicts, e.g. deployments) merge by index
                    # so that sections sharing a key combine their fields.
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
        for virt_key, real_key in self.virt_mappings.items():
            virt_data = merged.pop(virt_key, None)
            if virt_data is None:
                continue
            real_data = merged.get(real_key)
            if isinstance(virt_data, list) and isinstance(real_data, list):
                # Build lookup from virtual list entries
                config_by_name: dict[str, dict[str, Any]] = {}
                for entry in virt_data:
                    if isinstance(entry, dict):
                        for name in entry:
                            config_by_name[name] = entry
                # Replace plain string entries with config dicts
                for i, entry in enumerate(real_data):
                    if isinstance(entry, str) and entry in config_by_name:
                        real_data[i] = config_by_name[entry]
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
                    merged[real_key] = virt_data

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
            template_data=data.get("template_data", {}),
            stashed_data=data.get("stashed_data", {}),
            locked_services=data.get("locked_services", []),
            virt_mappings=data.get("virt_mappings", {}),
            staged_attachments=data.get("staged_attachments", {}),
        )
