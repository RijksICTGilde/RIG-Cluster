# Sub-part I: Package Init + Integration Tests

**Layer:** 3 (depends on all other sub-parts)
**Files to create:**
- `opi/forms/editables/__init__.py`
- `tests/test_editables_integration.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

This is the final assembly step. Create the package `__init__.py` that exports the public API, and write integration tests that verify the full pipeline from editable definition through to HTML rendering.

## __init__.py

```python
"""Editable-driven dynamic forms — declarative YAML path -> widget mapping."""

from __future__ import annotations

from opi.forms.editables.bridge import (
    editable_to_form_field,
    resolve_options_for_editable,
    should_render_editable,
)
from opi.forms.editables.editable import (
    EditableConverter,
    EditableEnforcer,
    EditableValidator,
    ProjectEditable,
)
from opi.forms.editables.flow import FlowMode, FormFlow
from opi.forms.editables.part import EditablePart
from opi.forms.editables.path import get_value, resolve_path, set_value

__all__ = [
    # Protocols
    "EditableConverter",
    "EditableValidator",
    "EditableEnforcer",
    # Core dataclasses
    "ProjectEditable",
    "EditablePart",
    "FormFlow",
    "FlowMode",
    # Path utilities
    "get_value",
    "set_value",
    "resolve_path",
    # Bridge functions
    "editable_to_form_field",
    "should_render_editable",
    "resolve_options_for_editable",
]
```

**CRITICAL:** Verify no circular imports. The import chain is:
```
__init__ → bridge → editable + path + field + providers (no cycles)
__init__ → editable (no deps on other editables modules)
__init__ → part → editable + layout (no cycles)
__init__ → flow → part (no cycles)
__init__ → path (standalone, no cycles)
```

---

## Integration Tests: test_editables_integration.py

These tests verify the full pipeline end-to-end. They use real components from multiple sub-parts together.

```python
"""
End-to-end integration tests for the editables package.

Tests the full pipeline: ProjectEditable -> editable_to_form_field() -> FormField -> ROOSWidgetAdapter.
"""
from __future__ import annotations

import pytest

from opi.forms.editables import (
    EditablePart,
    FlowMode,
    FormFlow,
    ProjectEditable,
    editable_to_form_field,
    get_value,
    resolve_path,
    set_value,
    should_render_editable,
)
from opi.forms.editables.converters import (
    EncryptedDisplayConverter,
    IntegerListConverter,
    TruncateConverter,
)
from opi.forms.editables.validators import RequiredValidator, SlugValidator
from opi.forms.editables.enforcers import AdminRequiredEnforcer
from opi.forms.widgets.roos import ROOSWidgetAdapter


class TestFullRenderPipeline:
    """Test editable -> FormField -> HTML rendering."""

    def test_simple_text_field(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        yaml_data = {"name": "test-project"}
        field = editable_to_form_field(editable, yaml_data)
        adapter = ROOSWidgetAdapter()
        html = adapter.render_field(field)
        assert "c-text-input-field" in html
        assert "test-project" in html

    def test_display_card_encrypted_field(self):
        editable = ProjectEditable(
            yaml_path="config/api-key",
            widget="display-card",
            label="API Key",
            readonly=True,
            converter=EncryptedDisplayConverter(),
        )
        yaml_data = {"config": {"api-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata..."}}
        field = editable_to_form_field(editable, yaml_data)
        adapter = ROOSWidgetAdapter()
        html = adapter.render_field(field)
        assert "c-card" in html
        assert "Versleuteld opgeslagen" in html
        # Must NOT contain actual encrypted data
        assert "BEGIN AGE" not in html

    def test_truncated_display_card(self):
        editable = ProjectEditable(
            yaml_path="config/age-public-key",
            widget="display-card",
            label="Public Key",
            readonly=True,
            converter=TruncateConverter(20),
        )
        yaml_data = {"config": {"age-public-key": "age1ufgl52y9y2aumys23l3e6zplekaw4j3ndk2yrwgfteq44fgd0qaq6zcrz5"}}
        field = editable_to_form_field(editable, yaml_data)
        assert field.value == "age1ufgl52y9y2aumys2..."
        assert len(field.value) < 30

    def test_select_with_options_provider(self):
        editable = ProjectEditable(
            yaml_path="components[0]/type",
            widget="select",
            label="Type",
            options_provider="ComponentTypeOptionsProvider",
        )
        yaml_data = {"components": [{"type": "single"}]}
        field = editable_to_form_field(editable, yaml_data)
        assert field.value == "single"
        assert field.options is not None
        assert len(field.options) > 0

    def test_sequence_item_with_index(self):
        editable = ProjectEditable(
            yaml_path="users[*]/email",
            widget="text",
            label="Email",
        )
        yaml_data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}

        field_0 = editable_to_form_field(editable, yaml_data, index=0)
        assert field_0.path == "users[0]/email"
        assert field_0.value == "a@b.c"

        field_1 = editable_to_form_field(editable, yaml_data, index=1)
        assert field_1.path == "users[1]/email"
        assert field_1.value == "d@e.f"


class TestConditionalVisibility:
    """Test should_render_editable with real dependency patterns."""

    def test_publish_on_web_depends_on_services(self):
        """Component publish-on-web only shown if service is enabled."""
        editable = ProjectEditable(
            yaml_path="components[*]/publish-on-web",
            widget="checkbox",
            label="Publiceren op web",
            depends_on="services",
            show_when={"contains": "publish-on-web"},
        )
        yaml_with = {"services": ["publish-on-web", "keycloak"]}
        yaml_without = {"services": ["keycloak"]}
        yaml_empty = {"services": []}

        assert should_render_editable(editable, yaml_with) is True
        assert should_render_editable(editable, yaml_without) is False
        assert should_render_editable(editable, yaml_empty) is False

    def test_sso_depends_on_keycloak(self):
        editable = ProjectEditable(
            yaml_path="components[*]/sso-rijk",
            widget="checkbox",
            label="SSO Rijk",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        assert should_render_editable(editable, {"services": ["keycloak"]}) is True
        assert should_render_editable(editable, {"services": ["redis"]}) is False

    def test_mixed_service_list_format(self):
        """Services can be mixed str/dict (YAML format)."""
        editable = ProjectEditable(
            yaml_path="x",
            widget="checkbox",
            label="X",
            depends_on="services",
            show_when={"contains": "keycloak"},
        )
        services = ["publish-on-web", {"keycloak": {"config": {"template": "sso"}}}]
        assert should_render_editable(editable, {"services": services}) is True


class TestConverterRoundTrips:
    """Test converter read -> write round trips."""

    def test_integer_list_round_trip(self):
        conv = IntegerListConverter()
        original = [80, 443, 8080]
        as_string = conv.read(original)
        assert as_string == "80, 443, 8080"
        back_to_list = conv.write(as_string)
        assert back_to_list == original


class TestValidationIntegration:
    """Test validators work with real data."""

    def test_slug_validator_on_project_name(self):
        validator = SlugValidator()
        assert validator.validate("my-project") == []
        assert len(validator.validate("My Project!")) > 0

    def test_required_validator(self):
        validator = RequiredValidator()
        assert validator.validate("hello") == []
        assert len(validator.validate("")) > 0
        assert len(validator.validate(None)) > 0


class TestEnforcerIntegration:
    """Test enforcers work with real data."""

    def test_admin_required_enforcer(self):
        enforcer = AdminRequiredEnforcer()
        users_with_admin = [
            {"email": "admin@example.nl", "role": "admin"},
            {"email": "dev@example.nl", "role": "developer"},
        ]
        result = enforcer.enforce(users_with_admin, {})
        assert result == users_with_admin

        users_no_admin = [{"email": "dev@example.nl", "role": "developer"}]
        with pytest.raises(ValueError):
            enforcer.enforce(users_no_admin, {})


class TestEditablePartComposition:
    """Test EditablePart grouping."""

    def test_identity_part(self):
        part = EditablePart(
            part_id="identity",
            title="Uw project",
            icon="huis",
            editables=[
                ProjectEditable(yaml_path="name", widget="text", label="project.name", required=True),
                ProjectEditable(yaml_path="display-name", widget="text", label="project.display_name", required=True),
                ProjectEditable(yaml_path="description", widget="textarea", label="project.description"),
                ProjectEditable(yaml_path="clusters", widget="checkbox-group", label="project.clusters", required=True),
            ],
            in_create_wizard=True,
            wizard_step=1,
        )
        assert part.part_id == "identity"
        assert len(part.editables) == 4
        assert part.wizard_step == 1

    def test_render_all_part_editables(self):
        """All editables in a part can be converted to FormFields."""
        part = EditablePart(
            part_id="identity",
            title="Project",
            editables=[
                ProjectEditable(yaml_path="name", widget="text", label="Naam"),
                ProjectEditable(yaml_path="description", widget="textarea", label="Omschrijving"),
            ],
        )
        yaml_data = {"name": "test", "description": "A test project"}
        for editable in part.editables:
            field = editable_to_form_field(editable, yaml_data)
            assert field.name == editable.yaml_path
            assert field.widget_type == editable.widget


class TestFormFlowComposition:
    """Test FormFlow composing parts."""

    def test_create_wizard_flow(self):
        flow = FormFlow(
            flow_id="create-project",
            title="Project Aanmaken",
            mode=FlowMode.WIZARD,
            parts=[
                EditablePart(part_id="identity", title="Uw project"),
                EditablePart(part_id="services", title="Services"),
                EditablePart(part_id="users", title="Team"),
                EditablePart(part_id="components", title="Componenten"),
            ],
            show_review=True,
        )
        assert len(flow.parts) == 4
        assert flow.mode == FlowMode.WIZARD

    def test_edit_tabs_flow(self):
        flow = FormFlow(
            flow_id="edit-project",
            title="Project Bewerken",
            mode=FlowMode.TABS,
            parts=[
                EditablePart(part_id="identity", title="Algemeen"),
                EditablePart(part_id="users", title="Team"),
            ],
            htmx_base_url="/projects/test/parts",
            save_per_part=True,
        )
        assert flow.mode == FlowMode.TABS
        assert flow.htmx_base_url == "/projects/test/parts"


class TestPathUtilitiesIntegration:
    """Test path utilities with realistic project data."""

    def test_extract_from_real_project_structure(self):
        project = {
            "name": "amt-odc-prd",
            "display-name": "AMT ODC Production",
            "clusters": ["odcn-production"],
            "users": [
                {"email": "admin@example.nl", "role": "admin"},
                {"email": "dev@example.nl", "role": "developer"},
            ],
            "components": [
                {
                    "name": "component-1",
                    "type": "single",
                    "ports": {"inbound": [8000], "outbound": [80, 443]},
                }
            ],
        }

        assert get_value(project, "name") == "amt-odc-prd"
        assert get_value(project, "display-name") == "AMT ODC Production"
        assert get_value(project, "users[0]/email") == "admin@example.nl"
        assert get_value(project, "users[*]/role") == ["admin", "developer"]
        assert get_value(project, "components[0]/ports/inbound") == [8000]

    def test_set_value_on_project_structure(self):
        project = {"name": "test", "users": [{"email": "a@b.c", "role": "admin"}]}
        set_value(project, "users[0]/email", "new@example.nl")
        assert project["users"][0]["email"] == "new@example.nl"

    def test_resolve_path_for_sequence_items(self):
        assert resolve_path("users[*]/email", 0) == "users[0]/email"
        assert resolve_path("deployments[*]/components[*]/image", 2) == "deployments[2]/components[*]/image"
```

## Verification

After creating these files, run:

```bash
cd /Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python

# All editable tests
uv run pytest tests/test_editables_*.py -v

# Linting
uv run ruff check opi/forms/editables/ --fix
uv run ruff format opi/forms/editables/

# Type checking
uv run pyright opi/forms/editables/

# Verify existing tests still pass
uv run pytest tests/ -v --ignore=tests/test_editables_*.py
```

## Code Style

- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
