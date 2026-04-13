"""Tests for backup schedule wizard flow: flow registration, section building, rendering.

Covers:
- get_flow() dynamic lookup for modal-edit-backup-schedule-N
- build_backup_schedule_flow() produces correct FormFlow
- build_backup_schedule_section() produces correct FormSection with RRULE fields
- BackupScheduleFrequencyOptionsProvider returns correct options
- Section renders with SELECT widgets for frequency, time, day, monthday
- Editable path materialization from [*] to [N]
- RRULE converter round-trip
"""

from __future__ import annotations

import pytest
from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.flows import (
    build_backup_schedule_flow,
    get_flow,
)
from opi.forms.visualizers.providers import (
    BackupScheduleFrequencyOptionsProvider,
    BackupScheduleOptionsProvider,
    BackupScheduleTimeOptionsProvider,
)
from opi.forms.visualizers.wizard_sections import build_backup_schedule_section
from opi.web.router_wizard import _render_step_html

# ---------------------------------------------------------------------------
# get_flow() dynamic lookup tests
# ---------------------------------------------------------------------------


class TestGetFlowBackupSchedule:
    """Tests for dynamic backup schedule flow lookup via get_flow()."""

    def test_returns_flow_for_index_0(self) -> None:
        flow = get_flow("modal-edit-backup-schedule-0")
        assert flow.flow_id == "modal-edit-backup-schedule-0"

    def test_returns_flow_for_index_1(self) -> None:
        flow = get_flow("modal-edit-backup-schedule-1")
        assert flow.flow_id == "modal-edit-backup-schedule-1"

    def test_returns_flow_for_index_5(self) -> None:
        flow = get_flow("modal-edit-backup-schedule-5")
        assert flow.flow_id == "modal-edit-backup-schedule-5"

    def test_raises_for_non_numeric_suffix(self) -> None:
        with pytest.raises(KeyError, match="Unknown flow"):
            get_flow("modal-edit-backup-schedule-abc")

    def test_raises_for_missing_suffix(self) -> None:
        with pytest.raises(KeyError, match="Unknown flow"):
            get_flow("modal-edit-backup-schedule-")


# ---------------------------------------------------------------------------
# build_backup_schedule_flow tests
# ---------------------------------------------------------------------------


class TestBuildBackupScheduleFlow:
    """Tests for the backup schedule flow builder."""

    def test_flow_id_contains_index(self) -> None:
        flow = build_backup_schedule_flow(2)
        assert flow.flow_id == "modal-edit-backup-schedule-2"

    def test_flow_title(self) -> None:
        flow = build_backup_schedule_flow(0)
        assert flow.title == "Backup schema instellen"

    def test_flow_has_one_section(self) -> None:
        flow = build_backup_schedule_flow(0)
        assert len(flow.sections) == 1

    def test_flow_no_review(self) -> None:
        flow = build_backup_schedule_flow(0)
        assert flow.show_review is False

    def test_section_id_matches_index(self) -> None:
        flow = build_backup_schedule_flow(3)
        section = flow.sections[0]
        assert section.section_id == "backup-schedule-3"


# ---------------------------------------------------------------------------
# build_backup_schedule_section tests
# ---------------------------------------------------------------------------


class TestBuildBackupScheduleSection:
    """Tests for the backup schedule section builder."""

    def test_section_id_contains_index(self) -> None:
        section = build_backup_schedule_section(0)
        assert section.section_id == "backup-schedule-0"

    def test_section_title(self) -> None:
        section = build_backup_schedule_section(0)
        assert section.title == "Backup schema instellen"

    def test_section_has_five_editables(self) -> None:
        section = build_backup_schedule_section(0)
        assert len(section.editables) == 5

    def test_first_editable_is_time_select(self) -> None:
        """Transient fields are processed first so the frequency converter can read them."""
        section = build_backup_schedule_section(0)
        vis = section.editables[0]
        assert vis.widget == WidgetType.SELECT
        assert vis.label == "Tijd (indicatie)"

    def test_second_editable_is_day_select(self) -> None:
        section = build_backup_schedule_section(0)
        vis = section.editables[1]
        assert vis.widget == WidgetType.SELECT
        assert vis.label == "Dag van de week"

    def test_third_editable_is_monthday_select(self) -> None:
        section = build_backup_schedule_section(0)
        vis = section.editables[2]
        assert vis.widget == WidgetType.SELECT
        assert vis.label == "Dag van de maand"

    def test_fourth_editable_is_resource_types_checkbox(self) -> None:
        section = build_backup_schedule_section(0)
        vis = section.editables[3]
        assert vis.widget == WidgetType.CHECKBOX_GROUP
        assert vis.label == "Resource types"

    def test_fifth_editable_is_frequency_select(self) -> None:
        """Frequency field is last so it can combine transient values into RRULE."""
        section = build_backup_schedule_section(0)
        vis = section.editables[4]
        assert vis.widget == WidgetType.SELECT
        assert vis.label == "Herhaling"

    def test_editable_path_materialized_index_0(self) -> None:
        section = build_backup_schedule_section(0)
        # Frequency is the last editable (index 4)
        vis = section.editables[4]
        assert "[0]" in vis.editable.yaml_path
        assert "[*]" not in vis.editable.yaml_path
        assert vis.editable.yaml_path == "deployments[0]/backup/schedule"

    def test_editable_path_materialized_index_3(self) -> None:
        section = build_backup_schedule_section(3)
        # Frequency is the last editable (index 4)
        vis = section.editables[4]
        assert "[3]" in vis.editable.yaml_path
        assert vis.editable.yaml_path == "deployments[3]/backup/schedule"

    def test_section_post_save_action(self) -> None:
        section = build_backup_schedule_section(0)
        assert section.post_save_action == "save_only"

    def test_section_layout_uses_concrete_index(self) -> None:
        section = build_backup_schedule_section(2)
        layout = section.layout
        assert isinstance(layout, list)
        assert any("deployments[2]" in str(item) for item in layout)

    def test_section_has_help_text(self) -> None:
        section = build_backup_schedule_section(0)
        vis = section.editables[0]
        assert vis.help_text is not None
        assert len(vis.help_text) > 0


# ---------------------------------------------------------------------------
# BackupScheduleFrequencyOptionsProvider tests
# ---------------------------------------------------------------------------


class TestBackupScheduleOptionsProvider:
    """Tests for the backup schedule frequency options provider."""

    def test_returns_list(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        assert isinstance(options, list)

    def test_returns_four_options(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        assert len(options) == 4

    def test_first_option_is_no_schedule(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        assert options[0]["value"] == ""
        assert "Geen" in options[0]["label"]

    def test_daily_option(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        values = {o["value"]: o["label"] for o in options}
        assert "DAILY" in values
        assert values["DAILY"] == "Dagelijks"

    def test_weekly_option(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        values = {o["value"]: o["label"] for o in options}
        assert "WEEKLY" in values
        assert values["WEEKLY"] == "Wekelijks"

    def test_monthly_option(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        options = provider.get_options()
        values = {o["value"]: o["label"] for o in options}
        assert "MONTHLY" in values
        assert values["MONTHLY"] == "Maandelijks"

    def test_all_options_have_value_and_label(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        for option in provider.get_options():
            assert "value" in option
            assert "label" in option

    def test_values_are_unique(self) -> None:
        provider = BackupScheduleFrequencyOptionsProvider()
        values = [o["value"] for o in provider.get_options()]
        assert len(values) == len(set(values))

    def test_legacy_provider_delegates(self) -> None:
        """BackupScheduleOptionsProvider delegates to frequency provider."""
        legacy = BackupScheduleOptionsProvider()
        freq = BackupScheduleFrequencyOptionsProvider()
        assert legacy.get_options() == freq.get_options()


class TestBackupScheduleTimeOptionsProvider:
    """Tests for the backup schedule time options provider."""

    def test_returns_48_options(self) -> None:
        provider = BackupScheduleTimeOptionsProvider()
        options = provider.get_options()
        assert len(options) == 48  # 24 hours * 2 (half-hour slots)

    def test_first_option_is_midnight(self) -> None:
        provider = BackupScheduleTimeOptionsProvider()
        options = provider.get_options()
        assert options[0]["value"] == "00:00"

    def test_last_option_is_2330(self) -> None:
        provider = BackupScheduleTimeOptionsProvider()
        options = provider.get_options()
        assert options[-1]["value"] == "23:30"


# ---------------------------------------------------------------------------
# Provider registry tests
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    """Tests that schedule providers are registered."""

    def test_frequency_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupScheduleFrequencyOptionsProvider" in PROVIDER_REGISTRY

    def test_time_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupScheduleTimeOptionsProvider" in PROVIDER_REGISTRY

    def test_day_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupScheduleDayOptionsProvider" in PROVIDER_REGISTRY

    def test_monthday_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupScheduleMonthDayOptionsProvider" in PROVIDER_REGISTRY

    def test_legacy_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupScheduleOptionsProvider" in PROVIDER_REGISTRY

    def test_resource_types_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupResourceTypesOptionsProvider" in PROVIDER_REGISTRY

    def test_backup_deployment_registered(self) -> None:
        from opi.forms.visualizers.providers import PROVIDER_REGISTRY

        assert "BackupDeploymentOptionsProvider" in PROVIDER_REGISTRY


# ---------------------------------------------------------------------------
# Section rendering tests
# ---------------------------------------------------------------------------


class TestBackupScheduleSectionRendering:
    """Tests for rendering the backup schedule section as HTML."""

    def test_renders_with_no_existing_schedule(self) -> None:
        section = build_backup_schedule_section(0)
        html = _render_step_html(section, yaml_data={"deployments": [{"name": "prod"}]})
        assert html
        assert "Herhaling" in html
        # Time field should be hidden when no schedule is set
        assert "Tijd (indicatie)" not in html

    def test_renders_with_existing_rrule_schedule(self) -> None:
        section = build_backup_schedule_section(0)
        yaml_data = {"deployments": [{"name": "prod", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}]}
        html = _render_step_html(section, yaml_data=yaml_data)
        assert html
        assert "DAILY" in html or "Dagelijks" in html

    def test_renders_select_element(self) -> None:
        section = build_backup_schedule_section(0)
        html = _render_step_html(section, yaml_data={"deployments": [{"name": "prod"}]})
        assert "<c-select-field" in html

    def test_renders_all_frequency_options(self) -> None:
        section = build_backup_schedule_section(0)
        html = _render_step_html(section, yaml_data={"deployments": [{"name": "prod"}]})
        assert "Dagelijks" in html
        assert "Wekelijks" in html
        assert "Maandelijks" in html

    def test_renders_for_different_deployment_index(self) -> None:
        section = build_backup_schedule_section(1)
        yaml_data = {
            "deployments": [
                {"name": "prod"},
                {"name": "staging", "backup": {"schedule": "FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU"}},
            ]
        }
        html = _render_step_html(section, yaml_data=yaml_data)
        assert html
        assert "Herhaling" in html


# ---------------------------------------------------------------------------
# Editable definition tests
# ---------------------------------------------------------------------------


class TestDeploymentBackupScheduleEditable:
    """Tests for the DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE definition."""

    def test_editable_exists(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

        assert DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE is not None

    def test_yaml_path(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

        assert DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE.yaml_path == "deployments[*]/backup/schedule"

    def test_values_provider(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

        assert DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE.values_provider == "BackupScheduleFrequencyOptionsProvider"

    def test_remove_when_none(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

        assert DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE.remove_when_none is True

    def test_has_converter(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

        assert isinstance(DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE.converter, RRuleFrequencyConverter)


# ---------------------------------------------------------------------------
# Visualizer definition tests
# ---------------------------------------------------------------------------


class TestDeploymentBackupScheduleVisualizer:
    """Tests for the DEPLOYMENT_BACKUP_SCHEDULE visualizer."""

    def test_visualizer_exists(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE is not None

    def test_widget_type(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE.widget == WidgetType.SELECT

    def test_label(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE.label == "Herhaling"

    def test_has_help_text(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE.help_text is not None

    def test_editable_reference(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE.editable is DEPLOYMENT_BACKUP_SCHEDULE_EDITABLE

    def test_has_data_rerender_attribute(self) -> None:
        """Frequency select must trigger re-render so dependent fields show/hide."""
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE

        assert DEPLOYMENT_BACKUP_SCHEDULE.attributes.get("data-rerender") == "true"


class TestBackupScheduleTimeDependsOn:
    """Time field should only show when a frequency is selected."""

    def test_time_editable_depends_on_schedule(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE

        assert DEPLOYMENT_BACKUP_SCHEDULE_TIME_EDITABLE.depends_on == "deployments[*]/backup/schedule"

    def test_time_hidden_when_no_schedule(self) -> None:
        """Time field should not render when schedule is empty."""
        from opi.forms.visualizers.bridge import should_render_editable
        from opi.forms.visualizers.wizard_sections import build_backup_schedule_section

        section = build_backup_schedule_section(0)
        time_vis = next(v for v in section.editables if "schedule:time" in v.editable.yaml_path)
        yaml_data = {"deployments": [{"name": "prod"}]}
        assert not should_render_editable(time_vis, yaml_data, index=0, siblings=section.editables)

    def test_time_visible_when_daily(self) -> None:
        """Time field should render when schedule has a frequency."""
        from opi.forms.visualizers.bridge import should_render_editable
        from opi.forms.visualizers.wizard_sections import build_backup_schedule_section

        section = build_backup_schedule_section(0)
        time_vis = next(v for v in section.editables if "schedule:time" in v.editable.yaml_path)
        yaml_data = {"deployments": [{"name": "prod", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}]}
        assert should_render_editable(time_vis, yaml_data, index=0, siblings=section.editables)

    def test_day_hidden_when_daily(self) -> None:
        """Day field should not render for daily schedule."""
        from opi.forms.visualizers.bridge import should_render_editable
        from opi.forms.visualizers.wizard_sections import build_backup_schedule_section

        section = build_backup_schedule_section(0)
        day_vis = next(v for v in section.editables if "schedule:day" in v.editable.yaml_path)
        yaml_data = {"deployments": [{"name": "prod", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}]}
        assert not should_render_editable(day_vis, yaml_data, index=0, siblings=section.editables)

    def test_day_visible_when_weekly(self) -> None:
        """Day field should render for weekly schedule."""
        from opi.forms.visualizers.bridge import should_render_editable
        from opi.forms.visualizers.wizard_sections import build_backup_schedule_section

        section = build_backup_schedule_section(0)
        day_vis = next(v for v in section.editables if "schedule:day" in v.editable.yaml_path)
        yaml_data = {
            "deployments": [{"name": "prod", "backup": {"schedule": "FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=MO"}}]
        }
        assert should_render_editable(day_vis, yaml_data, index=0, siblings=section.editables)


class TestBackupResourceTypesField:
    """Tests for the resource types checkbox group field."""

    def test_editable_yaml_path(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE

        assert DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE.yaml_path == "deployments[*]/backup/resource_types"

    def test_editable_depends_on_schedule(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE

        assert DEPLOYMENT_BACKUP_RESOURCE_TYPES_EDITABLE.depends_on == "deployments[*]/backup/schedule"

    def test_visualizer_is_checkbox_group(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENT_BACKUP_RESOURCE_TYPES

        assert DEPLOYMENT_BACKUP_RESOURCE_TYPES.widget == WidgetType.CHECKBOX_GROUP

    def test_resource_types_in_schedule_section(self) -> None:
        section = build_backup_schedule_section(0)
        paths = [v.editable.yaml_path for v in section.editables]
        assert "deployments[0]/backup/resource_types" in paths

    def test_resource_types_in_layout(self) -> None:
        section = build_backup_schedule_section(0)
        assert "deployments[0]/backup/resource_types" in section.layout

    def test_resource_types_hidden_when_no_schedule(self) -> None:
        from opi.forms.visualizers.bridge import should_render_editable

        section = build_backup_schedule_section(0)
        rt_vis = next(v for v in section.editables if "resource_types" in v.editable.yaml_path)
        yaml_data = {"deployments": [{"name": "prod"}]}
        assert not should_render_editable(rt_vis, yaml_data, index=0, siblings=section.editables)

    def test_resource_types_visible_when_schedule_set(self) -> None:
        from opi.forms.visualizers.bridge import should_render_editable

        section = build_backup_schedule_section(0)
        rt_vis = next(v for v in section.editables if "resource_types" in v.editable.yaml_path)
        yaml_data = {"deployments": [{"name": "prod", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}]}
        assert should_render_editable(rt_vis, yaml_data, index=0, siblings=section.editables)

    def test_resource_types_in_deployments_sequence(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENTS_SEQUENCE_EDITABLE

        child_paths = [c.yaml_path for c in DEPLOYMENTS_SEQUENCE_EDITABLE.children]
        assert "deployments[*]/backup/resource_types" in child_paths


class TestBackupResourceTypesOptionsProvider:
    """Tests for the BackupResourceTypesOptionsProvider."""

    def test_returns_all_without_context(self) -> None:
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        provider = BackupResourceTypesOptionsProvider()
        options = provider.get_options()
        assert isinstance(options, list)
        values = [o["value"] for o in options]
        assert "pvc" in values
        assert "database" in values
        assert "minio" in values

    def test_all_options_have_value_and_label(self) -> None:
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        for option in BackupResourceTypesOptionsProvider().get_options():
            assert "value" in option
            assert "label" in option

    def test_filters_by_deployment_path(self) -> None:
        """Schedule modal: only shows types the deployment uses."""
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        yaml_data = {
            "components": [
                {"name": "api", "services": ["persistent-storage"]},
            ],
            "deployments": [
                {"name": "prod", "components": [{"reference": "api"}]},
            ],
        }
        provider = BackupResourceTypesOptionsProvider(
            yaml_data=yaml_data, yaml_path="deployments[0]/backup/resource_types"
        )
        values = [o["value"] for o in provider.get_options()]
        assert "pvc" in values
        assert "database" not in values
        assert "minio" not in values

    def test_filters_with_database_service(self) -> None:
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        yaml_data = {
            "components": [
                {"name": "api", "services": ["persistent-storage", "postgresql-database"]},
            ],
            "deployments": [
                {"name": "prod", "components": [{"reference": "api"}]},
            ],
        }
        provider = BackupResourceTypesOptionsProvider(
            yaml_data=yaml_data, yaml_path="deployments[0]/backup/resource_types"
        )
        values = [o["value"] for o in provider.get_options()]
        assert "pvc" in values
        assert "database" in values
        assert "minio" not in values

    def test_resolves_deployment_from_form_data(self) -> None:
        """Manual backup: resolves deployment_name from form data."""
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        yaml_data = {
            "deployment_name": "staging",
            "components": [
                {"name": "api", "services": ["persistent-storage"]},
            ],
            "deployments": [
                {"name": "prod", "components": [{"reference": "api"}]},
                {"name": "staging", "components": [{"reference": "api"}]},
            ],
        }
        provider = BackupResourceTypesOptionsProvider(
            yaml_data=yaml_data,
            yaml_path="resource_types",
        )
        values = [o["value"] for o in provider.get_options()]
        assert "pvc" in values
        assert "database" not in values

    def test_empty_when_deployment_has_no_services(self) -> None:
        """Returns no options if deployment uses no backupable services."""
        from opi.forms.visualizers.providers import BackupResourceTypesOptionsProvider

        yaml_data = {
            "components": [{"name": "api", "services": []}],
            "deployments": [{"name": "prod", "components": [{"reference": "api"}]}],
        }
        provider = BackupResourceTypesOptionsProvider(
            yaml_data=yaml_data, yaml_path="deployments[0]/backup/resource_types"
        )
        values = [o["value"] for o in provider.get_options()]
        assert values == []


class TestBackupDeploymentOptionsProvider:
    """Tests for the BackupDeploymentOptionsProvider."""

    def test_empty_without_context(self) -> None:
        from opi.forms.visualizers.providers import BackupDeploymentOptionsProvider

        provider = BackupDeploymentOptionsProvider()
        assert provider.get_options() == []

    def test_returns_deployments_from_yaml_data(self) -> None:
        from opi.forms.visualizers.providers import BackupDeploymentOptionsProvider

        yaml_data = {
            "_cluster_deployments": [
                {"name": "production", "namespace": "rig-prod"},
                {"name": "staging", "namespace": "rig-staging"},
            ]
        }
        provider = BackupDeploymentOptionsProvider(yaml_data=yaml_data)
        options = provider.get_options()
        assert len(options) == 2
        assert options[0]["value"] == "production"
        assert options[1]["value"] == "staging"


class TestManualBackupEditables:
    """Tests for the manual backup transient editables."""

    def test_deployment_name_editable_is_transient(self) -> None:
        from opi.forms.editables.fields.deployments import BACKUP_DEPLOYMENT_NAME_EDITABLE

        assert BACKUP_DEPLOYMENT_NAME_EDITABLE.transient is True

    def test_deployment_name_yaml_path(self) -> None:
        from opi.forms.editables.fields.deployments import BACKUP_DEPLOYMENT_NAME_EDITABLE

        assert BACKUP_DEPLOYMENT_NAME_EDITABLE.yaml_path == "deployment_name"

    def test_resource_types_editable_is_transient(self) -> None:
        from opi.forms.editables.fields.deployments import BACKUP_RESOURCE_TYPES_EDITABLE

        assert BACKUP_RESOURCE_TYPES_EDITABLE.transient is True

    def test_resource_types_yaml_path(self) -> None:
        from opi.forms.editables.fields.deployments import BACKUP_RESOURCE_TYPES_EDITABLE

        assert BACKUP_RESOURCE_TYPES_EDITABLE.yaml_path == "resource_types"

    def test_backup_select_section_has_editables(self) -> None:
        from opi.forms.visualizers.wizard_sections import BACKUP_SELECT_SECTION

        assert len(BACKUP_SELECT_SECTION.editables) == 2

    def test_backup_select_section_deployment_name_first(self) -> None:
        from opi.forms.visualizers.wizard_sections import BACKUP_SELECT_SECTION

        assert BACKUP_SELECT_SECTION.editables[0].label == "Deployment"

    def test_backup_select_section_resource_types_second(self) -> None:
        from opi.forms.visualizers.wizard_sections import BACKUP_SELECT_SECTION

        vis = BACKUP_SELECT_SECTION.editables[1]
        assert vis.widget == WidgetType.CHECKBOX_GROUP
        assert vis.label == "Resource types"


# ---------------------------------------------------------------------------
# Deployments sequence integration tests
# ---------------------------------------------------------------------------


class TestDeploymentsSequenceIntegration:
    """Tests that backup schedule is included in the deployments sequence."""

    def test_backup_schedule_in_deployments_sequence(self) -> None:
        from opi.forms.visualizers.fields.deployments import DEPLOYMENTS_SEQUENCE

        child_paths = [c.editable.yaml_path for c in DEPLOYMENTS_SEQUENCE.children]
        assert "deployments[*]/backup/schedule" in child_paths

    def test_backup_schedule_editable_in_sequence(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENTS_SEQUENCE_EDITABLE

        child_paths = [c.yaml_path for c in DEPLOYMENTS_SEQUENCE_EDITABLE.children]
        assert "deployments[*]/backup/schedule" in child_paths

    def test_transient_fields_in_sequence(self) -> None:
        from opi.forms.editables.fields.deployments import DEPLOYMENTS_SEQUENCE_EDITABLE

        child_paths = [c.yaml_path for c in DEPLOYMENTS_SEQUENCE_EDITABLE.children]
        assert "deployments[*]/backup/schedule:time" in child_paths
        assert "deployments[*]/backup/schedule:day" in child_paths
        assert "deployments[*]/backup/schedule:monthday" in child_paths


# ---------------------------------------------------------------------------
# RRULE converter tests
# ---------------------------------------------------------------------------


class TestRRuleFrequencyConverter:
    """Tests for the RRULE frequency converter."""

    def test_read_daily(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        assert conv.read("FREQ=DAILY;BYHOUR=2;BYMINUTE=0") == "DAILY"

    def test_read_weekly(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        assert conv.read("FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU") == "WEEKLY"

    def test_read_empty(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        assert conv.read("") == ""
        assert conv.read(None) == ""

    def test_write_daily(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        result = conv.write("DAILY")
        assert result is not None
        assert "FREQ=DAILY" in result
        assert "BYHOUR=" in result
        assert "BYMINUTE=" in result

    def test_write_empty(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        assert conv.write("") is None
        assert conv.write(None) is None

    def test_view_daily(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        result = conv.view("FREQ=DAILY;BYHOUR=2;BYMINUTE=0")
        assert "Dagelijks" in result
        assert "02:00" in result

    def test_view_weekly(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        result = conv.view("FREQ=WEEKLY;BYHOUR=3;BYMINUTE=30;BYDAY=SU")
        assert "Wekelijks" in result
        assert "zondag" in result
        assert "03:30" in result

    def test_view_monthly(self) -> None:
        from opi.forms.editables.converters import RRuleFrequencyConverter

        conv = RRuleFrequencyConverter()
        result = conv.view("FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=15")
        assert "Maandelijks" in result
        assert "dag 15" in result
        assert "04:00" in result


class TestRRuleTimeConverter:
    """Tests for the RRULE time converter."""

    def test_read_extracts_time_from_context(self) -> None:
        from opi.forms.editables.converters import RRuleTimeConverter

        conv = RRuleTimeConverter()
        context = {"deployments": [{"backup": {"schedule": "FREQ=DAILY;BYHOUR=3;BYMINUTE=30"}}]}
        assert conv.read(None, context_data=context) == "03:30"

    def test_read_defaults_to_0200(self) -> None:
        from opi.forms.editables.converters import RRuleTimeConverter

        conv = RRuleTimeConverter()
        assert conv.read(None) == "02:00"
