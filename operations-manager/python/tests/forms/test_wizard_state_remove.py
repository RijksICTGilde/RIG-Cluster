"""Reproduces the modal-wizard sequence-remove bug.

The bug: when a user removes an item from a list the editable owns
(e.g. team members at yaml_path="users"), the removal was silently
discarded — because template_data held a pristine copy of the list and
get_merged_data's merge-by-index couldn't shrink it.

The fix is at the seam: a full-owner list belongs in step_data only,
never duplicated into template_data.
"""

from __future__ import annotations

from opi.forms.visualizers.flows import (
    MODAL_EDIT_COMPONENTS_FLOW,
    MODAL_EDIT_TEAM_FLOW,
)
from opi.forms.wizard.state import WizardState
from opi.web.router_detail_edit import _fully_owned_list_keys


def test_step_data_shrinks_when_not_also_in_template() -> None:
    """Without the shadow copy in template_data, get_merged_data naturally
    returns step_data's list — shorter, longer, or replaced as-is.
    """
    state = WizardState(
        flow_id="modal-edit-team",
        current_step="team",
        active_sections=["team"],
        project_name="pm-5sj",
        # The fix: template_data does NOT carry 'users' — it's fully owned by the
        # team section's editable, so step_data is the single source of truth.
        template_data={"name": "pm-5sj", "deployments": [{"name": "prod"}]},
    )
    state.store_step_data("team", {"users": [{"email": "keep@rijksoverheid.nl", "role": "admin"}]})

    assert state.get_merged_data()["users"] == [{"email": "keep@rijksoverheid.nl", "role": "admin"}]


def test_merge_by_index_still_works_for_shared_non_owned_lists() -> None:
    """Partial-field contributors (indexed yaml_paths like deployments[0]/name)
    still merge by index so multiple sections can combine fields into the
    same deployment entry. The fix must leave this untouched.
    """
    state = WizardState(
        flow_id="create-project",
        current_step="domain",
        active_sections=["identity", "domain"],
        # deployments is NOT fully owned by a single editable — sections write
        # to deployments[0]/name and deployments[0]/base-domain, so it stays
        # in template_data and get_merged_data combines fields.
        template_data={
            "deployments": [{"name": "prod", "cluster": "odcn", "base-domain": "old.example"}],
        },
    )
    state.store_step_data("identity", {"deployments": [{"name": "prod"}]})
    state.store_step_data("domain", {"deployments": [{"base-domain": "new.example"}]})

    merged = state.get_merged_data()

    assert merged["deployments"] == [{"name": "prod", "cluster": "odcn", "base-domain": "new.example"}]


def test_fully_owned_list_keys_team_flow() -> None:
    """The team modal flow fully owns the 'users' list via a bare yaml_path."""
    assert _fully_owned_list_keys(MODAL_EDIT_TEAM_FLOW) == {"users"}


def test_fully_owned_list_keys_components_flow() -> None:
    """The components modal flow fully owns the 'components' list."""
    assert "components" in _fully_owned_list_keys(MODAL_EDIT_COMPONENTS_FLOW)


def test_fully_owned_list_keys_ignores_indexed_paths() -> None:
    """A flow whose editables only use indexed paths (like deployments[0]/name)
    should report no fully-owned keys — those sections are partial contributors,
    and their lists must stay in template_data so merge-by-index preserves
    fields from other sections.
    """
    from types import SimpleNamespace

    from opi.forms.editables.editable import Editable, WidgetType
    from opi.forms.visualizers.visualizer import EditableVisualizer

    partial_editable = Editable(yaml_path="deployments[0]/name")
    vis = EditableVisualizer(editable=partial_editable, widget=WidgetType.TEXT, label="Name")
    flow = SimpleNamespace(sections=[SimpleNamespace(editables=[vis])])

    assert _fully_owned_list_keys(flow) == set()
