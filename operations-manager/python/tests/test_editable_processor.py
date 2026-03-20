"""Tests for EditableFormProcessor."""

from __future__ import annotations

from opi.forms.editables.converters import IntegerListConverter
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.validators import EmailValidator, SlugValidator
from opi.forms.visualizers.visualizer import EditableVisualizer

SAMPLE_YAML = {
    "name": "test-project",
    "display-name": "Test Project",
    "description": "Een test project",
    "users": [
        {"email": "admin@test.nl", "role": "admin"},
        {"email": "dev@test.nl", "role": "developer"},
    ],
    "components": [
        {
            "name": "frontend",
            "type": "single",
            "ports": {"inbound": [8080], "outbound": [443]},
        },
    ],
    "services": ["publish-on-web"],
    "config": {
        "age-public-key": "age1abc...",
        "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata",
    },
}


class TestValidateEditables:
    async def test_valid_data_returns_empty(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="name", validator=SlugValidator()),
                widget=WidgetType.TEXT,
                label="Naam",
            ),
        ]
        submitted = {"name": "valid-name"}
        yaml_data = {"name": ""}
        _, errors = await processor.process_json_submission(submitted, editables, yaml_data)
        assert errors == {}

    async def test_invalid_slug_returns_error(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="name", validator=SlugValidator()),
                widget=WidgetType.TEXT,
                label="Naam",
            ),
        ]
        submitted = {"name": "INVALID NAME!"}
        yaml_data = {"name": ""}
        _, errors = await processor.process_json_submission(submitted, editables, yaml_data)
        assert "name" in errors

    async def test_sequence_child_validation(self):
        processor = EditableFormProcessor()
        email_visualizer = EditableVisualizer(
            editable=Editable(yaml_path="users[*]/email", validator=EmailValidator()),
            widget=WidgetType.TEXT,
            label="Email",
        )
        seq_visualizer = EditableVisualizer(
            editable=Editable(yaml_path="users"),
            widget=WidgetType.SEQUENCE,
            label="Users",
            children=[email_visualizer],
        )
        submitted = {"users": [{"email": "valid@test.nl"}, {"email": "not-an-email"}]}
        yaml_data = submitted
        _, errors = await processor.process_json_submission(submitted, [seq_visualizer], yaml_data)
        assert "users[1]/email" in errors

    async def test_no_validator_no_error(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="description"),
                widget=WidgetType.TEXTAREA,
                label="Desc",
            ),
        ]
        submitted = {"description": "anything"}
        yaml_data = {"description": ""}
        _, errors = await processor.process_json_submission(submitted, editables, yaml_data)
        assert errors == {}


class TestApplyToYaml:
    async def test_writes_simple_value(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="display-name"),
                widget=WidgetType.TEXT,
                label="Naam",
            ),
        ]
        submitted = {"display-name": "Updated Name"}
        result, _ = await processor.process_json_submission(submitted, editables, SAMPLE_YAML)
        assert result["display-name"] == "Updated Name"
        # Original is unchanged
        assert SAMPLE_YAML["display-name"] == "Test Project"

    async def test_skips_readonly_fields(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="config/age-public-key"),
                widget=WidgetType.DISPLAY_CARD,
                label="Key",
                readonly=True,
            ),
        ]
        submitted = {"config": {"age-public-key": "hacked-value"}}
        result, _ = await processor.process_json_submission(submitted, editables, SAMPLE_YAML)
        assert result["config"]["age-public-key"] == "age1abc..."

    async def test_skips_readonly_on_edit_in_edit_mode(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="name"),
                widget=WidgetType.TEXT,
                label="Naam",
                readonly_on_edit=True,
            ),
        ]
        submitted = {"name": "new-name"}
        result, _ = await processor.process_json_submission(submitted, editables, SAMPLE_YAML, edit_mode=True)
        assert result["name"] == "test-project"

    async def test_allows_readonly_on_edit_in_create_mode(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="name"),
                widget=WidgetType.TEXT,
                label="Naam",
                readonly_on_edit=True,
            ),
        ]
        submitted = {"name": "new-name"}
        result, _ = await processor.process_json_submission(submitted, editables, SAMPLE_YAML, edit_mode=False)
        assert result["name"] == "new-name"

    async def test_applies_converter_write(self):
        processor = EditableFormProcessor()
        child_editables = [
            EditableVisualizer(
                editable=Editable(
                    yaml_path="components[*]/ports/inbound",
                    converter=IntegerListConverter(),
                ),
                widget=WidgetType.TEXT,
                label="Ports",
            ),
        ]
        seq_visualizer = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=child_editables,
        )
        submitted = {
            "components": [
                {
                    "name": "frontend",
                    "type": "single",
                    "ports": {"inbound": "8080, 9090", "outbound": [443]},
                }
            ]
        }
        result, _ = await processor.process_json_submission(submitted, [seq_visualizer], SAMPLE_YAML)
        assert result["components"][0]["ports"]["inbound"] == [8080, 9090]

    async def test_preserves_encrypted_fields(self):
        processor = EditableFormProcessor()
        editables = [
            EditableVisualizer(
                editable=Editable(yaml_path="config/age-private-key"),
                widget=WidgetType.DISPLAY_CARD,
                label="Key",
                readonly=True,
            ),
        ]
        original_key = SAMPLE_YAML["config"]["age-private-key"]
        result, _ = await processor.process_json_submission({}, editables, SAMPLE_YAML)
        assert result["config"]["age-private-key"] == original_key

    async def test_sequence_apply(self):
        processor = EditableFormProcessor()
        email_vis = EditableVisualizer(
            editable=Editable(yaml_path="users[*]/email"),
            widget=WidgetType.TEXT,
            label="Email",
        )
        role_vis = EditableVisualizer(
            editable=Editable(yaml_path="users[*]/role"),
            widget=WidgetType.SELECT,
            label="Role",
        )
        seq_vis = EditableVisualizer(
            editable=Editable(yaml_path="users"),
            widget=WidgetType.SEQUENCE,
            label="Users",
            children=[email_vis, role_vis],
        )
        submitted = {
            "users": [
                {"email": "new-admin@test.nl", "role": "admin"},
                {"email": "new-dev@test.nl", "role": "developer"},
            ]
        }
        result, _ = await processor.process_json_submission(submitted, [seq_vis], SAMPLE_YAML)
        assert result["users"][0]["email"] == "new-admin@test.nl"
        assert result["users"][1]["email"] == "new-dev@test.nl"


class TestNestedSequenceValidation:
    async def test_nested_sequence_validation(self):
        processor = EditableFormProcessor()
        comp_ref = EditableVisualizer(
            editable=Editable(
                yaml_path="deployments[*]/components[*]/reference",
                validator=SlugValidator(),
            ),
            widget=WidgetType.SELECT,
            label="Ref",
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="deployments[*]/components"),
            widget=WidgetType.SEQUENCE,
            label="Comps",
            children=[comp_ref],
        )
        dep_seq = EditableVisualizer(
            editable=Editable(yaml_path="deployments"),
            widget=WidgetType.SEQUENCE,
            label="Deps",
            children=[comp_seq],
        )
        submitted = {"deployments": [{"components": [{"reference": "valid"}, {"reference": "INVALID!"}]}]}
        yaml_data = submitted
        _, errors = await processor.process_json_submission(submitted, [dep_seq], yaml_data)
        assert "deployments[0]/components[1]/reference" in errors


class TestCheckboxGroupCoercion:
    """Ensure checkbox_group values are always stored as lists.

    HTMX sends a single string when only one checkbox is checked.
    The processor must coerce it to a list so downstream code never
    iterates over individual characters.
    """

    async def test_single_string_coerced_to_list_in_json_pipeline(self):
        processor = EditableFormProcessor()
        services_vis = EditableVisualizer(
            editable=Editable(yaml_path="components[*]/services"),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Services",
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=[services_vis],
        )
        submitted = {"components": [{"services": "publish-on-web"}]}
        yaml_data = {"components": [{"name": "web", "services": []}]}
        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert result["components"][0]["services"] == ["publish-on-web"]

    async def test_list_stays_list_in_json_pipeline(self):
        processor = EditableFormProcessor()
        services_vis = EditableVisualizer(
            editable=Editable(yaml_path="components[*]/services"),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Services",
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=[services_vis],
        )
        submitted = {"components": [{"services": ["publish-on-web", "keycloak"]}]}
        yaml_data = {"components": [{"name": "web", "services": []}]}
        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert result["components"][0]["services"] == ["publish-on-web", "keycloak"]

    async def test_none_coerced_to_empty_list(self):
        processor = EditableFormProcessor()
        services_vis = EditableVisualizer(
            editable=Editable(yaml_path="components[*]/services"),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Services",
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=[services_vis],
        )
        submitted = {"components": [{"name": "web"}]}
        yaml_data = {"components": [{"name": "web", "services": ["old"]}]}
        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert result["components"][0]["services"] == []


class TestCheckboxVirtualize:
    """Ensure checkboxes with virtualize read from virtual paths.

    Regression test: keycloak restrict-access checkbox has
    virtualize=("services", "_services-config") so the form submits
    under the virtual path. Without applying virtualize, the processor
    reads from the real path (which has no data) and the checkbox
    value is lost on rerender.
    """

    async def test_checkbox_reads_from_virtual_path(self):
        processor = EditableFormProcessor()
        checkbox_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="config/auth/enabled",
                virtualize=("config", "_form-config"),
            ),
            widget=WidgetType.CHECKBOX,
            label="Ingeschakeld",
        )
        # Form submits under virtual path (_form-config instead of config)
        submitted = {"_form-config": {"auth": {"enabled": True}}}
        yaml_data = {"config": {"auth": {"enabled": False}}}
        result, errors = await processor.process_json_submission(submitted, [checkbox_vis], yaml_data)
        assert errors == {}
        assert result["config"]["auth"]["enabled"] is True

    async def test_checkbox_falls_back_to_real_path(self):
        """When submitted data has no virtual key, fall back to real path."""
        processor = EditableFormProcessor()
        checkbox_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="config/auth/enabled",
                virtualize=("config", "_form-config"),
            ),
            widget=WidgetType.CHECKBOX,
            label="Ingeschakeld",
        )
        # Data already merged into real path (no virtual key present)
        submitted = {"config": {"auth": {"enabled": True}}}
        yaml_data = {"config": {"auth": {"enabled": False}}}
        result, errors = await processor.process_json_submission(submitted, [checkbox_vis], yaml_data)
        assert errors == {}
        assert result["config"]["auth"]["enabled"] is True

    async def test_checkbox_without_virtualize_unchanged(self):
        """Checkbox without virtualize reads from its yaml_path as before."""
        processor = EditableFormProcessor()
        checkbox_vis = EditableVisualizer(
            editable=Editable(yaml_path="config/enabled"),
            widget=WidgetType.CHECKBOX,
            label="Ingeschakeld",
        )
        submitted = {"config": {"enabled": True}}
        yaml_data = {"config": {"enabled": False}}
        result, errors = await processor.process_json_submission(submitted, [checkbox_vis], yaml_data)
        assert errors == {}
        assert result["config"]["enabled"] is True

    async def test_checkbox_group_reads_from_virtual_path(self):
        """CHECKBOX_GROUP with virtualize should also use virtual path."""
        processor = EditableFormProcessor()
        cbg_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="config/features",
                virtualize=("config", "_form-config"),
            ),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Features",
        )
        submitted = {"_form-config": {"features": ["sso", "mfa"]}}
        yaml_data = {"config": {"features": []}}
        result, errors = await processor.process_json_submission(submitted, [cbg_vis], yaml_data)
        assert errors == {}
        assert result["config"]["features"] == ["sso", "mfa"]

    async def test_checkbox_virtualize_in_group(self):
        """Checkbox with virtualize inside a GROUP widget."""
        processor = EditableFormProcessor()
        checkbox_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="config/auth/enabled",
                virtualize=("config", "_form-config"),
            ),
            widget=WidgetType.CHECKBOX,
            label="Ingeschakeld",
        )
        group_vis = EditableVisualizer(
            editable=Editable(yaml_path="config/auth"),
            widget=WidgetType.GROUP,
            label="Auth",
            children=[checkbox_vis],
        )
        submitted = {"_form-config": {"auth": {"enabled": True}}}
        yaml_data = {"config": {"auth": {"enabled": False}}}
        result, errors = await processor.process_json_submission(submitted, [group_vis], yaml_data)
        assert errors == {}
        assert result["config"]["auth"]["enabled"] is True


class TestHiddenDependsOnSkipped:
    """Nested sequence children with unmet depends_on must be skipped.

    Regression test: when a component has depends_on storage sequences
    (persistent-storage, temp-storage) but the project services don't
    include them, the processor must not create those entries in the
    component's services list via set_value auto-creation.
    """

    async def test_hidden_nested_sequence_not_added_to_services(self):
        """Storage config sequence should be skipped when project lacks
        the storage service, preventing phantom service entries."""
        processor = EditableFormProcessor()

        services_vis = EditableVisualizer(
            editable=Editable(yaml_path="components[*]/services"),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Services",
        )
        storage_name_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="components[*]/services{persistent-storage}/config[*]/name",
                required=True,
            ),
            widget=WidgetType.TEXT,
            label="Naam",
        )
        storage_seq_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="components[*]/services{persistent-storage}/config",
                depends_on="services",
                show_when={"contains": "persistent-storage"},
            ),
            widget=WidgetType.SEQUENCE,
            label="Persistente opslag",
            children=[storage_name_vis],
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=[services_vis, storage_seq_vis],
        )

        # Project services do NOT include persistent-storage
        submitted = {
            "services": ["publish-on-web", "keycloak"],
            "components": [{"name": "web", "services": ["publish-on-web"]}],
        }
        yaml_data = {
            "services": ["publish-on-web", "keycloak"],
            "components": [{"name": "web", "services": ["publish-on-web"]}],
        }

        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)

        comp_services = result["components"][0]["services"]
        assert "persistent-storage" not in str(comp_services), (
            f"persistent-storage should not be in component services, got: {comp_services}"
        )

    async def test_visible_nested_sequence_still_processed(self):
        """When the project DOES have the storage service, the nested
        sequence should still be processed normally."""
        processor = EditableFormProcessor()

        services_vis = EditableVisualizer(
            editable=Editable(yaml_path="components[*]/services"),
            widget=WidgetType.CHECKBOX_GROUP,
            label="Services",
        )
        storage_name_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="components[*]/services{persistent-storage}/config[*]/name",
            ),
            widget=WidgetType.TEXT,
            label="Naam",
        )
        storage_seq_vis = EditableVisualizer(
            editable=Editable(
                yaml_path="components[*]/services{persistent-storage}/config",
                depends_on="services",
                show_when={"contains": "persistent-storage"},
            ),
            widget=WidgetType.SEQUENCE,
            label="Persistente opslag",
            children=[storage_name_vis],
        )
        comp_seq = EditableVisualizer(
            editable=Editable(yaml_path="components"),
            widget=WidgetType.SEQUENCE,
            label="Components",
            children=[services_vis, storage_seq_vis],
        )

        # Project services DO include persistent-storage
        submitted = {
            "services": ["publish-on-web", "persistent-storage"],
            "components": [
                {
                    "name": "web",
                    "services": [
                        "publish-on-web",
                        {"persistent-storage": {"config": [{"name": "data"}]}},
                    ],
                }
            ],
        }
        yaml_data = {
            "services": ["publish-on-web", "persistent-storage"],
            "components": [
                {
                    "name": "web",
                    "services": [
                        "publish-on-web",
                        {"persistent-storage": {"config": [{"name": "data"}]}},
                    ],
                }
            ],
        }

        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)

        # The storage config should still be present
        comp_services = result["components"][0]["services"]
        has_storage = any(isinstance(s, dict) and "persistent-storage" in s for s in comp_services)
        assert has_storage, f"persistent-storage config should be preserved, got: {comp_services}"
