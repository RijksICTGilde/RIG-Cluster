"""Tests for editable-driven form rendering."""

from __future__ import annotations

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.project_registry import get_all_project_editables, get_project_form_layout
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.widgets.roos import ROOSWidgetAdapter

SAMPLE_YAML = {
    "name": "test-project",
    "display-name": "Test Project",
    "description": "Een test project",
    "clusters": ["local"],
    "users": [
        {"email": "admin@test.nl", "role": "admin"},
        {"email": "dev@test.nl", "role": "developer"},
    ],
    "services": ["publish-on-web", "keycloak"],
    "components": [
        {
            "name": "frontend",
            "ports": {"inbound": [8080], "outbound": [443]},
            "resources": {
                "cpu": {"request": "50m", "limit": "1"},
                "memory": {"request": "256Mi", "limit": "1Gi"},
            },
            "services": ["publish-on-web"],
            "aliases": {"APP_NAME": "frontend"},
        },
    ],
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "repository": "main-repo",
            "subdomain": "app",
            "components": [
                {"reference": "frontend", "image": "nginx:latest", "imagePullPolicy": "Always"},
            ],
        },
    ],
    "config": {
        "age-public-key": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
        "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata\n-----END AGE ENCRYPTED FILE-----",
        "api-key": "-----BEGIN AGE ENCRYPTED FILE-----\nsecret\n-----END AGE ENCRYPTED FILE-----",
    },
}


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


class TestRenderFromEditables:
    def test_produces_html(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
            action="/projects/edit/test-project",
        )
        assert isinstance(html, str)
        assert len(html) > 0
        assert "<form" in html

    def test_contains_field_values(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
        )
        assert "Test Project" in html
        assert "Een test project" in html

    def test_render_fields_from_editables_no_form_wrapper(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        layout = get_project_form_layout()
        html = renderer.render_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            layout=layout,
            edit_mode=True,
        )
        assert "<form" not in html
        assert "Test Project" in html


class TestBuildFieldsFromEditables:
    def test_builds_fields_dict(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=True,
        )
        assert isinstance(fields, dict)
        assert "display-name" in fields
        assert "description" in fields
        assert "clusters" in fields

    def test_readonly_fields_always_readonly(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=True,
        )
        age_key_field = fields["config/age-public-key"]
        assert age_key_field.readonly is True

    def test_non_readonly_field_editable_on_create(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
            edit_mode=False,
        )
        display_name_field = fields["display-name"]
        assert display_name_field.readonly is False


class TestSequenceRendering:
    def test_users_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        users_field = fields["users"]
        assert users_field.widget_type == "sequence"
        assert len(users_field.children) == 2  # 2 users in sample data

    def test_users_sequence_item_children(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        users_field = fields["users"]
        first_item = users_field.children[0]
        assert first_item.widget_type == "sequence_item"
        assert len(first_item.children) == 2  # email + role
        child_names = [c.name for c in first_item.children]
        assert "users[0]/email" in child_names
        assert "users[0]/role" in child_names

    def test_components_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        comp_field = fields["components"]
        assert comp_field.widget_type == "sequence"
        assert len(comp_field.children) == 1  # 1 component

    def test_components_item_has_children_without_storage_services(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        comp_field = fields["components"]
        first_item = comp_field.children[0]
        # 13 children: storage sequences hidden (no storage services),
        # path/rewrite-path always visible
        assert len(first_item.children) == 13

    def test_components_item_has_storage_with_storage_services(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        yaml_with_storage = {**SAMPLE_YAML, "services": [*SAMPLE_YAML["services"], "persistent-storage"]}
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=yaml_with_storage,
        )
        comp_field = fields["components"]
        first_item = comp_field.children[0]
        # 14 children: storage sequence visible (persistent-storage enabled),
        # path/rewrite-path always visible
        assert len(first_item.children) == 14


class TestNestedSequenceRendering:
    def test_deployments_sequence_field(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        assert dep_field.widget_type == "sequence"
        assert len(dep_field.children) == 1  # 1 deployment

    def test_deployment_item_contains_nested_sequence(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        first_dep = dep_field.children[0]
        # Find the nested sequence among children
        nested_seqs = [c for c in first_dep.children if c.widget_type == "sequence"]
        assert len(nested_seqs) == 1
        nested_seq = nested_seqs[0]
        assert nested_seq.name == "deployments[0]/components"
        assert len(nested_seq.children) == 1  # 1 deployment component

    def test_nested_sequence_item_has_children(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        dep_field = fields["deployments"]
        first_dep = dep_field.children[0]
        nested_seqs = [c for c in first_dep.children if c.widget_type == "sequence"]
        nested_seq = nested_seqs[0]
        first_comp = nested_seq.children[0]
        assert first_comp.widget_type == "sequence_item"
        assert len(first_comp.children) == 3  # reference, image, pullPolicy


class TestDisplayCardRendering:
    def test_encrypted_fields_show_status(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        api_key_field = fields["config/api-key"]
        assert api_key_field.widget_type == "display_card"
        assert api_key_field.readonly is True
        assert "Versleuteld" in str(api_key_field.value)

    def test_truncated_field_shows_ellipsis(self):
        renderer = _create_renderer()
        editables = get_all_project_editables()
        fields = renderer._build_fields_from_editables(
            editables=editables,
            yaml_data=SAMPLE_YAML,
        )
        age_pub_field = fields["config/age-public-key"]
        assert age_pub_field.widget_type == "display_card"
        assert "..." in str(age_pub_field.value)


SAMPLE_YAML_WITH_KEYCLOAK = {
    **SAMPLE_YAML,
    "services": [
        "publish-on-web",
        {
            "keycloak": {
                "config": {
                    "template": "sso-only",
                    "additional_redirect_uris": [
                        "http://localhost:8080/*",
                        "http://localhost:3000/*",
                    ],
                    "restrict-access": {
                        "enabled": True,
                        "realm-role": "allowed-user",
                        "error-message": "${accessDeniedNoPermission}",
                    },
                    "additional-clients": [
                        {"name": "api-client", "redirect-uris": ["http://localhost:9090/*"]},
                    ],
                    "realm-roles": [
                        {"name": "admin", "description": "Administrator"},
                    ],
                },
            },
        },
    ],
}


class TestKeycloakConfigRendering:
    def test_keycloak_section_renders_all_fields(self):
        """All keycloak editables render with service config data."""
        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        renderer = _create_renderer()
        assert KEYCLOAK_CONFIG_SECTION.layout is not None
        html = renderer.render_fields_from_editables(
            editables=KEYCLOAK_CONFIG_SECTION.editables,
            yaml_data=SAMPLE_YAML_WITH_KEYCLOAK,
            layout=KEYCLOAK_CONFIG_SECTION.layout,
        )
        assert "Keycloak template" in html
        assert "Redirect URI" in html
        assert "Toegang beperken" in html

    def test_redirect_uris_sequence_has_items(self):
        """Redirect URIs render as a sequence with items."""
        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        renderer = _create_renderer()
        fields = renderer._build_fields_from_editables(
            editables=KEYCLOAK_CONFIG_SECTION.editables,
            yaml_data=SAMPLE_YAML_WITH_KEYCLOAK,
        )
        uris_field = fields.get("services/keycloak/config/additional_redirect_uris")
        assert uris_field is not None
        assert uris_field.widget_type == "sequence"
        assert len(uris_field.children) == 2  # 2 URIs in sample data

    def test_restrict_access_subfields_visible_when_enabled(self):
        """Realm role and error message are visible when restrict-access is enabled."""
        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        renderer = _create_renderer()
        fields = renderer._build_fields_from_editables(
            editables=KEYCLOAK_CONFIG_SECTION.editables,
            yaml_data=SAMPLE_YAML_WITH_KEYCLOAK,
        )
        assert "services/keycloak/config/restrict-access/realm-role" in fields
        assert "services/keycloak/config/restrict-access/error-message" in fields

    def test_restrict_access_subfields_hidden_when_disabled(self):
        """Realm role and error message are hidden when restrict-access is disabled."""
        import copy

        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        data = copy.deepcopy(SAMPLE_YAML_WITH_KEYCLOAK)
        data["services"][1]["keycloak"]["config"]["restrict-access"]["enabled"] = False

        renderer = _create_renderer()
        fields = renderer._build_fields_from_editables(
            editables=KEYCLOAK_CONFIG_SECTION.editables,
            yaml_data=data,
        )
        assert "services/keycloak/config/restrict-access/realm-role" not in fields
        assert "services/keycloak/config/restrict-access/error-message" not in fields

    def test_additional_clients_sequence(self):
        """Additional clients render as a sequence."""
        from opi.forms.visualizers.wizard_sections import KEYCLOAK_CONFIG_SECTION

        renderer = _create_renderer()
        fields = renderer._build_fields_from_editables(
            editables=KEYCLOAK_CONFIG_SECTION.editables,
            yaml_data=SAMPLE_YAML_WITH_KEYCLOAK,
        )
        clients_field = fields.get("services/keycloak/config/additional-clients")
        assert clients_field is not None
        assert clients_field.widget_type == "sequence"
        assert len(clients_field.children) == 1  # 1 client in sample data


SAMPLE_YAML_WITH_DOMAIN = {
    **SAMPLE_YAML,
    "deployments": [
        {
            **SAMPLE_YAML["deployments"][0],
            "domain-format": "component-deployment-project",
            "subdomain": "mijnapp",
            "base-domain": "rijksapp.nl",
            "root-component": "frontend",
        },
    ],
}


class TestDomainSectionRendering:
    def test_domain_section_renders_fields(self):
        from opi.forms.visualizers.wizard_sections import DOMAIN_SECTION

        renderer = _create_renderer()
        assert DOMAIN_SECTION.layout is not None
        html = renderer.render_fields_from_editables(
            editables=DOMAIN_SECTION.editables,
            yaml_data=SAMPLE_YAML_WITH_DOMAIN,
            layout=DOMAIN_SECTION.layout,
        )
        assert "URL-formaat" in html

    def test_domain_section_has_group_editable_with_five_children(self):
        from opi.forms.editables.editable import WidgetType
        from opi.forms.visualizers.wizard_sections import DOMAIN_SECTION

        # Section has one group parent editable
        assert len(DOMAIN_SECTION.editables) == 1
        group = DOMAIN_SECTION.editables[0]
        assert group.widget == WidgetType.GROUP
        # Group wraps 5 child domain fields
        assert len(group.children) == 5


class TestConditionalVisibility:
    def test_hidden_editable_not_in_fields(self):
        """An editable with depends_on that isn't satisfied should be skipped."""
        renderer = _create_renderer()
        editable = EditableVisualizer(
            editable=Editable(
                yaml_path="conditional-field",
                depends_on="services",
                show_when={"contains": "nonexistent-service"},
            ),
            widget=WidgetType.TEXT,
            label="Conditional",
        )
        fields = renderer._build_fields_from_editables(
            editables=[editable],
            yaml_data=SAMPLE_YAML,
        )
        assert "conditional-field" not in fields


class TestTextareaValueRendering:
    """Verify the textarea widget renders the stored value correctly."""

    def test_identity_section_textarea_contains_description(self):
        """The description textarea must contain the value from yaml_data."""
        from opi.forms.visualizers.wizard_sections import IDENTITY_SECTION

        yaml_data = {
            "display-name": "Mijn Project",
            "description": "Dit is de omschrijving",
            "clusters": ["sandboxed-local"],
        }
        renderer = _create_renderer()
        html = renderer.render_fields_from_editables(
            editables=IDENTITY_SECTION.editables,
            yaml_data=yaml_data,
            layout=IDENTITY_SECTION.layout,
        )
        assert "Mijn Project" in html, "display-name should be in rendered HTML"
        assert "Dit is de omschrijving" in html, f"description value should be in rendered HTML, got:\n{html}"

    def test_textarea_field_has_value(self):
        """The description FormField must have the value populated."""
        from opi.forms.visualizers.wizard_sections import IDENTITY_SECTION

        yaml_data = {
            "display-name": "Test",
            "description": "Mijn omschrijving",
            "clusters": ["sandboxed-local"],
        }
        renderer = _create_renderer()
        fields = renderer._build_fields_from_editables(
            editables=IDENTITY_SECTION.editables,
            yaml_data=yaml_data,
        )
        desc_field = fields["description"]
        assert desc_field.widget_type == "textarea"
        assert desc_field.value == "Mijn omschrijving", f"Expected 'Mijn omschrijving', got {desc_field.value!r}"

    def test_textarea_renders_value_in_tag_content(self):
        """The c-textarea-field tag must contain the value as inner text."""
        editable = EditableVisualizer(
            editable=Editable(yaml_path="description"),
            widget=WidgetType.TEXTAREA,
            label="Omschrijving",
        )
        yaml_data = {"description": "Hallo wereld"}
        renderer = _create_renderer()
        html = renderer.render_fields_from_editables(
            editables=[editable],
            yaml_data=yaml_data,
            layout=["description"],
        )
        assert ">Hallo wereld</c-textarea-field>" in html, f"Expected value between textarea tags, got:\n{html}"

        """An editable whose dependency is satisfied should be included."""
        renderer = _create_renderer()
        editable = EditableVisualizer(
            editable=Editable(
                yaml_path="conditional-field",
                depends_on="services",
                show_when={"contains": "keycloak"},
            ),
            widget=WidgetType.TEXT,
            label="Conditional",
        )
        fields = renderer._build_fields_from_editables(
            editables=[editable],
            yaml_data=SAMPLE_YAML,
        )
        assert "conditional-field" in fields
