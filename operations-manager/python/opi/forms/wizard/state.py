"""Wizard state management for multi-step form flows.

WizardState holds intermediate form data as the user navigates wizard steps.
WizardSteps provides structured navigation context for templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    """Static project template data (repositories, etc.) — lowest priority layer."""

    locked_services: list[str] = field(default_factory=list)
    """Services that existed in the project before the wizard started.
    These cannot be removed and are rendered as locked in the service cards."""

    stashed_data: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Step data parked when a conditional section becomes inactive.

    Keyed by section_id, same structure as step_data.  When the section
    becomes active again the data is restored so previously entered
    config is not lost.
    """

    def get_merged_data(self) -> dict[str, Any]:
        """Merge template and step data into a single dict.

        Merge order (later overrides earlier):
        1. template_data (static skeleton — repositories, base config)
        2. step_data per active section (user-entered form values)
        """
        merged: dict[str, Any] = {}
        if self.template_data:
            merged.update(self.template_data)
        for section_id in self.active_sections:
            if section_id in self.step_data:
                merged.update(self.step_data[section_id])
        return merged

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
        )
