# Sub-part G: Bridge Functions

**Layer:** 2 (depends on Sub-parts A and B)
**Files to create:**
- `opi/forms/editables/bridge.py`
- `tests/test_editables_bridge.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

The bridge connects `ProjectEditable` instances to the existing `FormField` rendering pipeline. This is the integration point — it takes an editable + YAML data and produces a `FormField` that the existing `FormRenderer` and `ROOSWidgetAdapter` can render.

## Dependencies

- `opi.forms.editables.editable.ProjectEditable` (Sub-part A)
- `opi.forms.editables.path.get_value`, `resolve_path` (Sub-part B)
- `opi.forms.field.FormField` (existing)
- `opi.forms.providers.get_provider`, `PROVIDER_REGISTRY` (existing)

---

## Functions

### editable_to_form_field

```python
def editable_to_form_field(
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    index: int | None = None,
    edit_mode: bool = False,
) -> FormField:
    """
    Convert a ProjectEditable + YAML data into a FormField for rendering.

    This bridges the editable system into the existing FormField -> WidgetAdapter pipeline.

    Args:
        editable: The field definition.
        yaml_data: Full project YAML dict.
        errors: Validation errors keyed by resolved path.
        index: Sequence index for [*] paths.
        edit_mode: Whether we're editing an existing project.

    Returns:
        A FormField ready for rendering.
    """
```

**Implementation steps:**

```python
# 1. Resolve the path
concrete_path = resolve_path(editable.yaml_path, index)

# 2. Extract value from YAML
raw_value = get_value(yaml_data, concrete_path)

# 3. Apply converter for display
display_value = raw_value
if editable.converter:
    display_value = editable.converter.view(raw_value)

# 4. Resolve options
options = resolve_options_for_editable(editable)

# 5. Build HTMX attrs dict
htmx_attrs: dict[str, str] = {}
if editable.htmx_trigger:
    htmx_attrs["hx-trigger"] = editable.htmx_trigger
if editable.htmx_target:
    htmx_attrs["hx-target"] = editable.htmx_target
if editable.htmx_swap:
    htmx_attrs["hx-swap"] = editable.htmx_swap

# 6. Determine readonly
readonly = editable.readonly or (editable.readonly_on_edit and edit_mode)

# 7. Build FormField
return FormField(
    name=concrete_path,           # Used as HTML name="" attribute
    path=concrete_path,           # Full path for error lookup
    schema_type=str,              # Default — sufficient for rendering
    widget_type=editable.widget,
    label=editable.label,
    required=editable.required,
    description=editable.description,
    placeholder=editable.placeholder,
    value=display_value,
    options=options,
    errors=(errors or {}).get(concrete_path, []),
    readonly=readonly,
    readonly_on_edit=editable.readonly_on_edit,
    min_items=editable.min_items,
    max_items=editable.max_items,
    htmx_attrs=htmx_attrs,
)
```

**CRITICAL:** Do NOT set `FormField.converter`. The editable converter's `view()` is already applied to the value. The existing async `Converter` protocol in `FormField` is not used.

### should_render_editable

```python
def should_render_editable(
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    index: int | None = None,
) -> bool:
    """
    Check if an editable should be rendered based on its dependencies.

    Implements 3 dependency patterns:

    1. No depends_on -> always render (True)
    2. depends_on set, no show_when -> render if dependency value is truthy
    3. depends_on + show_when -> evaluate conditions:
       - {"contains": "value"} -> dep_value is list and "value" in dep_value
       - {"field": ["val1", "val2"]} -> dep_value in ["val1", "val2"]
       - {"field": "value"} -> dep_value == "value"
    """
```

**Implementation:**

```python
if not editable.depends_on:
    return True

# Get the dependency value
dep_value = get_value(yaml_data, editable.depends_on)

if editable.show_when is None:
    # Just check if dependency is truthy
    return bool(dep_value)

# Evaluate show_when conditions
for key, expected in editable.show_when.items():
    if key == "contains":
        # Check if dep_value (a list) contains the expected item
        if not isinstance(dep_value, list):
            return False
        # Handle mixed str/dict lists (services format)
        names = _extract_names_from_list(dep_value)
        if expected not in names:
            return False
    else:
        # Check field value match
        if isinstance(expected, list):
            if dep_value not in expected:
                return False
        else:
            if dep_value != expected:
                return False

return True
```

**Helper for mixed service list names:**

```python
def _extract_names_from_list(items: list) -> list[str]:
    """Extract names from a mixed str/dict list (services format)."""
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.extend(item.keys())
    return names
```

### resolve_options_for_editable

```python
def resolve_options_for_editable(
    editable: ProjectEditable,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve dynamic options using the PROVIDER_REGISTRY.

    Args:
        editable: The editable whose options to resolve.
        context: Optional kwargs to pass to the provider constructor.

    Returns:
        List of option dicts, or empty list if no provider.
    """
    if not editable.options_provider:
        return []

    kwargs = context or {}
    try:
        provider = get_provider(editable.options_provider, **kwargs)
        return provider.get_options()
    except KeyError:
        return []
```

---

## Tests: test_editables_bridge.py

```python
class TestEditableToFormField:
    def test_simple_text_editable(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        yaml_data = {"name": "test-project"}
        field = editable_to_form_field(editable, yaml_data)
        assert field.name == "name"
        assert field.path == "name"
        assert field.widget_type == "text"
        assert field.label == "Naam"
        assert field.value == "test-project"
        assert field.schema_type is str
        assert field.converter is None  # MUST be None

    def test_with_converter_view(self):
        """converter.view() is applied to the YAML value."""
        from opi.forms.editables.converters import EncryptedDisplayConverter
        editable = ProjectEditable(
            yaml_path="config/api-key", widget="display-card", label="API Key",
            converter=EncryptedDisplayConverter(),
        )
        yaml_data = {"config": {"api-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndata"}}
        field = editable_to_form_field(editable, yaml_data)
        assert field.value == "Versleuteld opgeslagen"

    def test_missing_value_returns_none(self):
        editable = ProjectEditable(yaml_path="missing", widget="text", label="Missing")
        field = editable_to_form_field(editable, {})
        assert field.value is None

    def test_with_errors(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        errors = {"name": ["Dit veld is verplicht"]}
        field = editable_to_form_field(editable, {}, errors=errors)
        assert field.errors == ["Dit veld is verplicht"]

    def test_with_index_resolves_path(self):
        editable = ProjectEditable(yaml_path="users[*]/email", widget="text", label="Email")
        yaml_data = {"users": [{"email": "a@b.c"}, {"email": "d@e.f"}]}
        field = editable_to_form_field(editable, yaml_data, index=1)
        assert field.path == "users[1]/email"
        assert field.value == "d@e.f"

    def test_readonly_on_edit(self):
        editable = ProjectEditable(
            yaml_path="name", widget="text", label="Naam", readonly_on_edit=True,
        )
        field_create = editable_to_form_field(editable, {"name": "x"}, edit_mode=False)
        field_edit = editable_to_form_field(editable, {"name": "x"}, edit_mode=True)
        assert field_create.readonly is False
        assert field_edit.readonly is True

    def test_readonly_always(self):
        editable = ProjectEditable(
            yaml_path="ns", widget="text", label="NS", readonly=True,
        )
        field = editable_to_form_field(editable, {"ns": "x"}, edit_mode=False)
        assert field.readonly is True

    def test_htmx_attrs_mapped(self):
        editable = ProjectEditable(
            yaml_path="services", widget="service-cards", label="Services",
            htmx_trigger="change", htmx_target="#config", htmx_swap="innerHTML",
        )
        field = editable_to_form_field(editable, {})
        assert field.htmx_attrs["hx-trigger"] == "change"
        assert field.htmx_attrs["hx-target"] == "#config"
        assert field.htmx_attrs["hx-swap"] == "innerHTML"

    def test_description_and_placeholder(self):
        editable = ProjectEditable(
            yaml_path="name", widget="text", label="Naam",
            description="Help text", placeholder="mijn-project",
        )
        field = editable_to_form_field(editable, {})
        assert field.description == "Help text"
        assert field.placeholder == "mijn-project"


class TestShouldRenderEditable:
    def test_no_dependency_always_true(self):
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        assert should_render_editable(editable, {}) is True

    def test_dependency_exists_truthy(self):
        editable = ProjectEditable(
            yaml_path="x", widget="text", label="X", depends_on="flag",
        )
        assert should_render_editable(editable, {"flag": True}) is True
        assert should_render_editable(editable, {"flag": "yes"}) is True

    def test_dependency_missing_false(self):
        editable = ProjectEditable(
            yaml_path="x", widget="text", label="X", depends_on="flag",
        )
        assert should_render_editable(editable, {}) is False

    def test_dependency_falsy_false(self):
        editable = ProjectEditable(
            yaml_path="x", widget="text", label="X", depends_on="flag",
        )
        assert should_render_editable(editable, {"flag": False}) is False
        assert should_render_editable(editable, {"flag": ""}) is False
        assert should_render_editable(editable, {"flag": []}) is False

    def test_show_when_contains_match(self):
        editable = ProjectEditable(
            yaml_path="x", widget="checkbox", label="X",
            depends_on="services", show_when={"contains": "keycloak"},
        )
        assert should_render_editable(editable, {"services": ["keycloak", "redis"]}) is True

    def test_show_when_contains_no_match(self):
        editable = ProjectEditable(
            yaml_path="x", widget="checkbox", label="X",
            depends_on="services", show_when={"contains": "keycloak"},
        )
        assert should_render_editable(editable, {"services": ["redis"]}) is False

    def test_show_when_contains_mixed_list(self):
        """Services can be mixed str/dict lists."""
        editable = ProjectEditable(
            yaml_path="x", widget="checkbox", label="X",
            depends_on="services", show_when={"contains": "keycloak"},
        )
        services = ["publish-on-web", {"keycloak": {"config": {}}}]
        assert should_render_editable(editable, {"services": services}) is True

    def test_show_when_value_list_match(self):
        editable = ProjectEditable(
            yaml_path="path", widget="text", label="Path",
            depends_on="components[0]/type",
            show_when={"type": ["single", "frontend"]},
        )
        assert should_render_editable(editable, {"components": [{"type": "single"}]}) is True

    def test_show_when_value_list_no_match(self):
        editable = ProjectEditable(
            yaml_path="path", widget="text", label="Path",
            depends_on="components[0]/type",
            show_when={"type": ["single", "frontend"]},
        )
        assert should_render_editable(editable, {"components": [{"type": "backend"}]}) is False


class TestResolveOptionsForEditable:
    def test_no_provider_returns_empty(self):
        editable = ProjectEditable(yaml_path="x", widget="text", label="X")
        assert resolve_options_for_editable(editable) == []

    def test_known_provider_returns_options(self):
        editable = ProjectEditable(
            yaml_path="cluster", widget="select", label="Cluster",
            options_provider="CpuLimitOptionsProvider",
        )
        options = resolve_options_for_editable(editable)
        assert len(options) > 0
        assert all("value" in o for o in options)

    def test_unknown_provider_returns_empty(self):
        editable = ProjectEditable(
            yaml_path="x", widget="select", label="X",
            options_provider="NonExistentProvider",
        )
        assert resolve_options_for_editable(editable) == []
```

## Code Style

- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
