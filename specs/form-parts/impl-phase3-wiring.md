# Phase 3: Wire Up Editables-Driven Project Form

## Context

The editables infrastructure (`opi/forms/editables/`) is fully built — dataclasses, path utilities, converters, validators, enforcers, bridge functions, providers — but **nothing uses it**. The existing self-service portal form (`self-service-portal.html.j2`) is ~900 lines of hard-coded HTML with inline JavaScript. It works for project creation, but:

- Hard to maintain and extend (every new field = hand-coded HTML + JS)
- No project **editing** form exists (the edit route in `router_project_form.py` uses an old Pydantic approach and isn't even wired into the app)
- Fields like storage configuration, keycloak config, deployment subdomain details are missing

**Goal:** Replace the hard-coded form approach with editable-driven form generation. Start with **edit** (existing projects), then wire into create. One form for now — can split into wizard/tabs later.

**Focus areas:** Services, components, deployments, and their values. NOT repositories, AGE keys, or auto-generated fields.

---

## Architecture Decision

**Approach: Pre-convert editables to FormFields, reuse existing layout pipeline.**

The `FormRenderer._render_layout_element()` (renderer.py:288) already works with a `dict[str, FormField]`. It doesn't care where the FormFields came from — Pydantic or editables. We add a `render_from_editables()` method that:

1. Converts `ProjectEditable` instances → `FormField` via the existing `editable_to_form_field()` bridge
2. Builds a `fields_by_name` dict
3. Feeds it to the existing layout rendering pipeline

No new renderer class needed. Everything downstream (ROOSWidgetAdapter, layout elements) stays unchanged.

### Rendering Pipeline

```
editables + yaml_data
    → [editable_to_form_field(e, yaml_data, errors, edit_mode) for each editable]
    → dict[str, FormField]  (keyed by resolved yaml_path)
    → FormRenderer._render_layout_element(layout, fields_by_name)
    → ROOSWidgetAdapter.render_*(field)
    → HTML
```

### Sequence Rendering

The existing `_extract_sequence_children()` in `extractor.py:168` creates:
- A parent `FormField(widget_type="sequence")` with `.children` being
- Wrapper `FormField(widget_type="sequence_item")` entries, each containing sub-field `FormField` objects

The `Sequence` layout element in `renderer.py:331` expects this exact structure:
```python
if isinstance(element, Sequence):
    field = fields.get(element.field_name)
    if field and field.children:
        for i, child_field in enumerate(field.children):
            child_fields_html = [self.adapter.render_field(cf) for cf in child_field.children]
```

We must replicate this structure when converting sequence editables to FormFields.

### Save Pipeline (Reverse Flow)

```
form_data (flat key=value from HTML)
    → parse_form_data() → dict keyed by YAML paths
    → validate_editables() → errors dict (or empty)
    → enforce_parts() → global errors (or empty)
    → apply_to_yaml() → deep-copy yaml_data with values written via set_value()
    → save_project_file() → YAML to disk
```

---

## Existing Infrastructure Reference

### Files that are fully built and should be reused as-is:

| File | What it provides |
|------|-----------------|
| `opi/forms/editables/editable.py` | `ProjectEditable` dataclass, `EditableConverter`/`EditableValidator`/`EditableEnforcer` protocols |
| `opi/forms/editables/part.py` | `EditablePart` dataclass |
| `opi/forms/editables/flow.py` | `FormFlow`, `FlowMode` |
| `opi/forms/editables/path.py` | `get_value()`, `set_value()`, `resolve_path()` |
| `opi/forms/editables/bridge.py` | `editable_to_form_field()`, `should_render_editable()`, `resolve_options_for_editable()` |
| `opi/forms/editables/converters.py` | `EncryptedDisplayConverter`, `TruncateConverter`, `ServiceListConverter`, `NewlineSeparatedListConverter`, `IntegerListConverter`, `KeyValueConverter`, `CloneFromDisplayConverter`, `DeploymentServicesDisplayConverter`, `KeycloakRealmsDisplayConverter` |
| `opi/forms/editables/validators.py` | `SlugValidator`, `EmailValidator`, `MinMaxLengthValidator`, `RangeValidator`, `RequiredValidator` |
| `opi/forms/editables/enforcers.py` | `AdminRequiredEnforcer`, `UniqueNamesEnforcer`, `ServiceDependencyEnforcer` |
| `opi/forms/field.py` | `FormField` dataclass |
| `opi/forms/renderer.py` | `FormRenderer` with `_render_layout_element()`, `_translate_fields()`, `_apply_edit_mode()` |
| `opi/forms/layout.py` | `Row`, `Column`, `Fieldset`, `Sequence`, `Div`, `HTML`, `Hidden`, `Submit`, `ButtonGroup` |
| `opi/forms/widgets/roos.py` | `ROOSWidgetAdapter` — renders all widget types including `render_service_cards()`, `render_display_card()`, `render_sequence()`, `render_sequence_item()` |
| `opi/forms/providers.py` | 14 providers in `PROVIDER_REGISTRY`: `ClusterOptionsProvider`, `ServiceOptionsProvider`, `ComponentTypeOptionsProvider`, `UserRoleOptionsProvider`, `CpuLimitOptionsProvider`, `MemoryLimitOptionsProvider`, `StorageSizeOptionsProvider`, `FilteredServiceOptionsProvider`, `ComponentReferenceOptionsProvider`, `RepositoryOptionsProvider`, `PullPolicyOptionsProvider`, `BaseDomainOptionsProvider`, etc. |
| `opi/forms/i18n.py` | `DictTranslator`, `get_default_nl_translator()`, `DEFAULT_NL_TRANSLATIONS` dict |
| `opi/services/project_service.py` | Singleton `ProjectService` with `get_project()` returning `Project` object with `.data` (full YAML dict), `.filename` |
| `opi/templates/base.html.j2` | Base template with ROOS header, menubar, footer |
| `opi/templates/project-edit-form.html.j2` | Basic edit form template (breadcrumb + `{{ form_html | process_components }}`) |

### Files that exist but need to be rewritten:

| File | Current state | What needs to change |
|------|--------------|---------------------|
| `opi/web/router_project_form.py` | Uses Pydantic `ProjectFileModel`, POST has `# TODO: Save`. Router is NOT included in the app. | Rewrite to use editables. Wire into app. |

### File that needs a one-line addition:

| File | Change |
|------|--------|
| `opi/web/router.py` | Add `web_router.include_router(project_form_router)` after line 34 |

---

## Implementation Steps

### Step 1: Project Editable Definitions (Registry) ✅ DONE

**New file:** `opi/forms/editables/project_registry.py`

**Root directory for all paths:** `operations-manager/python/`

This file declares all `ProjectEditable` instances for the project YAML structure. Only user-editable and display-worthy fields — NOT auto-generated fields like repository URLs, git credentials, or AGE key generation.

#### Identity Section Editables

```python
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import SlugValidator, MinMaxLengthValidator, EmailValidator, RequiredValidator
from opi.forms.editables.converters import (
    EncryptedDisplayConverter, TruncateConverter, ServiceListConverter,
    IntegerListConverter, KeyValueConverter,
)
from opi.forms.editables.enforcers import AdminRequiredEnforcer, UniqueNamesEnforcer
from opi.forms.editables.part import EditablePart
from opi.forms.layout import Fieldset, Row, Column, Sequence, Submit, ButtonGroup

# --- Identity ---
NAME = ProjectEditable(
    yaml_path="name",
    widget="text",
    label="Projectnaam (technisch)",
    description="Technische identificatie, kan niet gewijzigd worden",
    required=True,
    readonly_on_edit=True,
    validator=SlugValidator(),
)

DISPLAY_NAME = ProjectEditable(
    yaml_path="display-name",
    widget="text",
    label="Weergavenaam",
    description="Een beschrijvende naam voor uw project",
    required=True,
    placeholder="Mijn Nieuwe Applicatie",
    validator=MinMaxLengthValidator(3, 100),
)

DESCRIPTION = ProjectEditable(
    yaml_path="description",
    widget="textarea",
    label="Projectomschrijving",
    description="Korte beschrijving van het doel en de scope van het project",
    placeholder="Dit project heeft als doel...",
)

CLUSTERS = ProjectEditable(
    yaml_path="clusters",
    widget="checkbox_group",
    label="Clusters",
    description="Selecteer de clusters waar dit project op draait",
    options_provider="ClusterOptionsProvider",
    required=True,
)
```

#### Team Section Editables

```python
USER_EMAIL = ProjectEditable(
    yaml_path="users[*]/email",
    widget="text",
    label="E-mailadres",
    required=True,
    placeholder="naam@organisatie.nl",
    validator=EmailValidator(),
)

USER_ROLE = ProjectEditable(
    yaml_path="users[*]/role",
    widget="select",
    label="Rol",
    required=True,
    options_provider="UserRoleOptionsProvider",
)

USERS_SEQUENCE = ProjectEditable(
    yaml_path="users",
    widget="sequence",
    label="Projectleden",
    min_items=1,
    children=[USER_EMAIL, USER_ROLE],
)
```

#### Services Section

```python
SERVICES = ProjectEditable(
    yaml_path="services",
    widget="service_cards",
    label="Beschikbare Services",
    description="Selecteer de services die u wilt activeren voor uw project",
    converter=ServiceListConverter(),
    options_provider="ServiceOptionsProvider",
)
```

#### Components Section (Sequence with Nested Fields)

```python
COMPONENT_NAME = ProjectEditable(
    yaml_path="components[*]/name",
    widget="text",
    label="Naam",
    required=True,
    validator=SlugValidator(),
)

COMPONENT_TYPE = ProjectEditable(
    yaml_path="components[*]/type",
    widget="select",
    label="Type",
    required=True,
    options_provider="ComponentTypeOptionsProvider",
)

COMPONENT_PORTS_INBOUND = ProjectEditable(
    yaml_path="components[*]/ports/inbound",
    widget="text",
    label="Inbound poorten",
    description="Kommagescheiden lijst van poorten (bijv. 8000, 8080)",
    converter=IntegerListConverter(),
)

COMPONENT_PORTS_OUTBOUND = ProjectEditable(
    yaml_path="components[*]/ports/outbound",
    widget="text",
    label="Outbound poorten",
    description="Kommagescheiden lijst (bijv. 80, 443)",
    converter=IntegerListConverter(),
)

COMPONENT_RESOURCES_CPU = ProjectEditable(
    yaml_path="components[*]/resources/cpu",
    widget="select",
    label="CPU limiet",
    options_provider="CpuLimitOptionsProvider",
)

COMPONENT_RESOURCES_MEMORY = ProjectEditable(
    yaml_path="components[*]/resources/memory",
    widget="select",
    label="Geheugen limiet",
    options_provider="MemoryLimitOptionsProvider",
)

COMPONENT_USES_SERVICES = ProjectEditable(
    yaml_path="components[*]/uses-services",
    widget="checkbox_group",
    label="Gebruikte services",
    description="Welke project-services gebruikt dit component",
    options_provider="FilteredServiceOptionsProvider",
)

COMPONENT_ALIASES = ProjectEditable(
    yaml_path="components[*]/aliases",
    widget="textarea",
    label="Aliassen",
    description="Variabele aliassen in KEY=VALUE formaat",
    converter=KeyValueConverter(),
)

COMPONENTS_SEQUENCE = ProjectEditable(
    yaml_path="components",
    widget="sequence",
    label="Componenten",
    min_items=1,
    children=[
        COMPONENT_NAME, COMPONENT_TYPE,
        COMPONENT_PORTS_INBOUND, COMPONENT_PORTS_OUTBOUND,
        COMPONENT_RESOURCES_CPU, COMPONENT_RESOURCES_MEMORY,
        COMPONENT_USES_SERVICES, COMPONENT_ALIASES,
    ],
)
```

#### Deployments Section (Sequence, Partially Read-Only)

```python
DEPLOYMENT_NAME = ProjectEditable(
    yaml_path="deployments[*]/name",
    widget="text",
    label="Deployment naam",
    required=True,
    readonly_on_edit=True,
)

DEPLOYMENT_CLUSTER = ProjectEditable(
    yaml_path="deployments[*]/cluster",
    widget="select",
    label="Cluster",
    required=True,
    options_provider="ClusterOptionsProvider",
)

DEPLOYMENT_REPOSITORY = ProjectEditable(
    yaml_path="deployments[*]/repository",
    widget="select",
    label="Repository",
    options_provider="RepositoryOptionsProvider",
)

DEPLOYMENT_SUBDOMAIN = ProjectEditable(
    yaml_path="deployments[*]/subdomain",
    widget="text",
    label="Subdomein",
    description="Optioneel subdomein voor deze deployment",
)

DEPLOYMENT_COMP_REFERENCE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/reference",
    widget="select",
    label="Component",
    required=True,
    options_provider="ComponentReferenceOptionsProvider",
)

DEPLOYMENT_COMP_IMAGE = ProjectEditable(
    yaml_path="deployments[*]/components[*]/image",
    widget="text",
    label="Container image",
    required=True,
    placeholder="nginx:latest",
)

DEPLOYMENT_COMP_PULL_POLICY = ProjectEditable(
    yaml_path="deployments[*]/components[*]/imagePullPolicy",
    widget="select",
    label="Pull policy",
    options_provider="PullPolicyOptionsProvider",
)

DEPLOYMENT_COMPONENTS_SEQ = ProjectEditable(
    yaml_path="deployments[*]/components",
    widget="sequence",
    label="Deployment componenten",
    min_items=1,
    children=[DEPLOYMENT_COMP_REFERENCE, DEPLOYMENT_COMP_IMAGE, DEPLOYMENT_COMP_PULL_POLICY],
)

DEPLOYMENTS_SEQUENCE = ProjectEditable(
    yaml_path="deployments",
    widget="sequence",
    label="Deployments",
    children=[
        DEPLOYMENT_NAME, DEPLOYMENT_CLUSTER, DEPLOYMENT_REPOSITORY,
        DEPLOYMENT_SUBDOMAIN, DEPLOYMENT_COMPONENTS_SEQ,
    ],
)
```

#### Config Section (Read-Only Display)

```python
AGE_PUBLIC_KEY = ProjectEditable(
    yaml_path="config/age-public-key",
    widget="display_card",
    label="AGE publieke sleutel",
    readonly=True,
    converter=TruncateConverter(20),
)

AGE_PRIVATE_KEY = ProjectEditable(
    yaml_path="config/age-private-key",
    widget="display_card",
    label="AGE privé sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)

API_KEY = ProjectEditable(
    yaml_path="config/api-key",
    widget="display_card",
    label="API sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)
```

#### Public Functions

```python
def get_all_project_editables() -> list[ProjectEditable]:
    """Return flat list of all top-level editables for the project form."""
    return [
        NAME, DISPLAY_NAME, DESCRIPTION, CLUSTERS,
        USERS_SEQUENCE,
        SERVICES,
        COMPONENTS_SEQUENCE,
        DEPLOYMENTS_SEQUENCE,
        AGE_PUBLIC_KEY, AGE_PRIVATE_KEY, API_KEY,
    ]


def get_project_form_layout() -> list[LayoutElement]:
    """Return layout definition for the single-page project form."""
    return [
        Fieldset(legend="Projectgegevens", children=[
            Row(children=[
                Column(child="name", width=4),
                Column(child="display-name", width=8),
            ]),
            "description",
            "clusters",
        ]),
        Fieldset(legend="Projectleden", children=[
            Sequence(field_name="users"),
        ]),
        Fieldset(legend="Services", children=[
            "services",
        ]),
        Fieldset(legend="Componenten", children=[
            Sequence(field_name="components"),
        ]),
        Fieldset(legend="Deployments", children=[
            Sequence(field_name="deployments"),
        ]),
        Fieldset(legend="Configuratie", description="Automatisch gegenereerde configuratie (alleen-lezen)", children=[
            "config/age-public-key",
            "config/age-private-key",
            "config/api-key",
        ]),
        ButtonGroup(buttons=[
            Submit(label="Opslaan", kind="primary", icon="opslaan"),
        ]),
    ]
```

**Important notes for implementation:**
- The layout field references (strings like `"name"`, `"display-name"`) must match the `FormField.name` which comes from the editable's resolved `yaml_path`
- The `Sequence(field_name="users")` must match the parent sequence FormField's name which is `"users"` (the yaml_path of `USERS_SEQUENCE`)
- Dutch labels are used directly on the editables (not i18n keys) for simplicity. If i18n is needed later, swap to key-based labels.

---

### Step 2: Editable-Aware Rendering ✅ DONE

**Modify:** `opi/forms/renderer.py`

Add two new methods to `FormRenderer`:

```python
def render_from_editables(
    self,
    editables: list[ProjectEditable],
    yaml_data: dict[str, Any],
    layout: LayoutElement | list[LayoutElement],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
    form_id: str = "form",
    action: str = "",
    method: str = "post",
    enctype: str | None = None,
    htmx_attrs: dict[str, str] | None = None,
) -> str:
    """Render a complete form from editable definitions + YAML data."""

def render_fields_from_editables(
    self,
    editables: list[ProjectEditable],
    yaml_data: dict[str, Any],
    layout: LayoutElement | list[LayoutElement],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
) -> str:
    """Render form fields without form wrapper (for HTMX partial updates)."""
```

#### Core Logic: `_build_fields_from_editables()`

Extract the shared logic into a private method:

```python
def _build_fields_from_editables(
    self,
    editables: list[ProjectEditable],
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
) -> dict[str, FormField]:
    """Convert editables to FormField dict for the layout pipeline."""
    from opi.forms.editables.bridge import editable_to_form_field, should_render_editable
    from opi.forms.editables.path import get_value

    errors = errors or {}
    fields_by_name: dict[str, FormField] = {}

    for editable in editables:
        # Skip fields whose visibility conditions aren't met
        if not should_render_editable(editable, yaml_data):
            continue

        if editable.widget == "sequence":
            # Build sequence FormField with children
            seq_field = self._build_sequence_field(editable, yaml_data, errors, edit_mode)
            fields_by_name[editable.yaml_path] = seq_field
        else:
            # Simple field
            form_field = editable_to_form_field(editable, yaml_data, errors, edit_mode=edit_mode)
            fields_by_name[form_field.path] = form_field

    # Apply translations and edit mode
    all_fields = list(fields_by_name.values())
    self._translate_fields(all_fields)
    if edit_mode:
        self._apply_edit_mode(all_fields)

    return fields_by_name
```

#### Sequence Building Logic

```python
def _build_sequence_field(
    self,
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]],
    edit_mode: bool,
) -> FormField:
    """Build a FormField for a sequence editable with item children."""
    from opi.forms.editables.bridge import editable_to_form_field
    from opi.forms.editables.path import get_value

    # Get the list items from YAML
    items = get_value(yaml_data, editable.yaml_path) or []
    if not isinstance(items, list):
        items = []

    # Create wrapper FormField for the sequence
    seq_field = FormField(
        name=editable.yaml_path,
        path=editable.yaml_path,
        schema_type=list,
        widget_type="sequence",
        label=editable.label or "",
        description=editable.description,
        min_items=editable.min_items,
        max_items=editable.max_items,
    )

    # For each item, create a sequence_item wrapper with children
    children: list[FormField] = []
    for index in range(len(items)):
        item_children: list[FormField] = []
        for child_editable in (editable.children or []):
            if child_editable.widget == "sequence":
                # Nested sequence (e.g., deployment components within deployments)
                nested_seq = self._build_nested_sequence_field(
                    child_editable, yaml_data, errors, edit_mode, parent_index=index,
                )
                item_children.append(nested_seq)
            else:
                child_field = editable_to_form_field(
                    child_editable, yaml_data, errors, index=index, edit_mode=edit_mode,
                )
                item_children.append(child_field)

        # Wrapper FormField matching _extract_sequence_children pattern
        item_field = FormField(
            name=f"{editable.yaml_path}[{index}]",
            path=f"{editable.yaml_path}[{index}]",
            schema_type=dict,
            widget_type="sequence_item",
            label=f"Item {index + 1}",
            required=False,
            children=item_children,
        )
        children.append(item_field)

    seq_field.children = children
    return seq_field
```

**Nested sequence handling** (for deployments[*]/components):

```python
def _build_nested_sequence_field(
    self,
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]],
    edit_mode: bool,
    parent_index: int,
) -> FormField:
    """Build a nested sequence (e.g., deployments[0]/components)."""
    from opi.forms.editables.path import get_value, resolve_path

    # Resolve the concrete path for this parent index
    concrete_path = resolve_path(editable.yaml_path, parent_index)
    items = get_value(yaml_data, concrete_path) or []
    if not isinstance(items, list):
        items = []

    nested_field = FormField(
        name=concrete_path,
        path=concrete_path,
        schema_type=list,
        widget_type="sequence",
        label=editable.label or "",
        min_items=editable.min_items,
        max_items=editable.max_items,
    )

    children: list[FormField] = []
    for child_index in range(len(items)):
        item_children: list[FormField] = []
        for child_editable in (editable.children or []):
            # For nested sequences, we need to resolve [*] twice:
            # first for parent_index, then for child_index
            # The child editable path is like "deployments[*]/components[*]/reference"
            # After parent resolution: "deployments[0]/components[*]/reference"
            # We need to pass both indices. Use resolve_path with parent first,
            # then editable_to_form_field with child index.
            child_field = editable_to_form_field(
                child_editable, yaml_data, errors,
                index=child_index, edit_mode=edit_mode,
            )
            # But the path needs the parent index too — this requires
            # temporarily resolving the first [*] before passing to bridge.
            # IMPLEMENTATION NOTE: This may require extending editable_to_form_field
            # or pre-resolving the parent [*] in the editable's yaml_path.
            # Simplest approach: create a copy of the child editable with
            # yaml_path pre-resolved for the parent index.
            item_children.append(child_field)

        item_field = FormField(
            name=f"{concrete_path}[{child_index}]",
            path=f"{concrete_path}[{child_index}]",
            schema_type=dict,
            widget_type="sequence_item",
            label=f"Item {child_index + 1}",
            required=False,
            children=item_children,
        )
        children.append(item_field)

    nested_field.children = children
    return nested_field
```

**Important implementation note on nested sequences:**
The `resolve_path()` function only replaces the FIRST `[*]`. For double-nested paths like `deployments[*]/components[*]/reference`, you need two resolution passes. The cleanest approach is to create a helper that replaces `[*]` at a specific depth, or to pre-resolve the parent `[*]` by creating a modified copy of child editables with `yaml_path = resolve_path(original_path, parent_index)` before passing to `editable_to_form_field()` with the child index.

---

### Step 3: Form Data Processor ✅ DONE

**New file:** `opi/forms/editables/processor.py`

Handles the reverse flow: form submission → validation → YAML update.

```python
from __future__ import annotations

import copy
from typing import Any

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.part import EditablePart
from opi.forms.editables.path import get_value, set_value, resolve_path


class EditableFormProcessor:
    """Processes form submissions through the editables pipeline."""

    def parse_form_data(
        self,
        form_data: Any,  # FastAPI's ImmutableMultiDict
        editables: list[ProjectEditable],
    ) -> dict[str, Any]:
        """
        Parse flat HTML form data into a dict keyed by YAML paths.

        HTML form names use the YAML path format (e.g., "users[0]/email",
        "components[1]/ports/inbound"). This method reads all known paths
        from form_data and returns them in a flat dict.

        Multi-value fields (checkboxes) use "path[]" naming convention.

        Returns:
            dict mapping yaml_path → submitted value
        """
        parsed: dict[str, Any] = {}

        for key in form_data:
            # Handle multi-value fields (e.g., "clusters[]", "services[]")
            if key.endswith("[]"):
                parsed[key.rstrip("[]")] = form_data.getlist(key)
            else:
                parsed[key] = form_data.get(key)

        return parsed

    def validate_editables(
        self,
        parsed: dict[str, Any],
        editables: list[ProjectEditable],
        yaml_data: dict[str, Any],
    ) -> dict[str, list[str]]:
        """
        Run each editable's validator on the parsed form data.

        For sequence editables, validates each item's child editables.

        Returns:
            dict mapping yaml_path → list of error messages.
            Empty dict means no errors.
        """
        errors: dict[str, list[str]] = {}

        for editable in editables:
            if editable.widget == "sequence":
                # Validate children for each item
                items = get_value(yaml_data, editable.yaml_path) or []
                for index in range(len(items)):
                    for child in (editable.children or []):
                        if child.validator:
                            concrete_path = resolve_path(child.yaml_path, index)
                            value = parsed.get(concrete_path)
                            field_errors = child.validator.validate(value)
                            if field_errors:
                                errors[concrete_path] = field_errors
            elif editable.validator:
                value = parsed.get(editable.yaml_path)
                field_errors = editable.validator.validate(value)
                if field_errors:
                    errors[editable.yaml_path] = field_errors

        return errors

    def enforce_parts(
        self,
        yaml_data: dict[str, Any],
        parts: list[EditablePart],
    ) -> list[str]:
        """
        Run part-level enforcers.

        Returns:
            List of global error messages. Empty means all passed.
        """
        global_errors: list[str] = []
        for part in parts:
            if part.enforcer:
                try:
                    part.enforcer.enforce(yaml_data)
                except ValueError as e:
                    global_errors.append(str(e))
        return global_errors

    def apply_to_yaml(
        self,
        parsed: dict[str, Any],
        editables: list[ProjectEditable],
        yaml_data: dict[str, Any],
        edit_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Write validated form values back into the YAML dict.

        - Deep-copies yaml_data first (preserves original)
        - Skips readonly fields
        - Skips readonly_on_edit fields when edit_mode=True
        - Applies converter.write() before set_value()

        Returns:
            New yaml_data dict with values applied.
        """
        result = copy.deepcopy(yaml_data)

        for editable in editables:
            if editable.readonly:
                continue
            if editable.readonly_on_edit and edit_mode:
                continue

            if editable.widget == "sequence":
                # Handle sequence items
                self._apply_sequence_to_yaml(editable, parsed, result, edit_mode)
            else:
                value = parsed.get(editable.yaml_path)
                if value is not None:
                    if editable.converter:
                        value = editable.converter.write(value)
                    set_value(result, editable.yaml_path, value)

        return result

    def _apply_sequence_to_yaml(
        self,
        editable: ProjectEditable,
        parsed: dict[str, Any],
        yaml_data: dict[str, Any],
        edit_mode: bool,
    ) -> None:
        """Apply sequence field values back to YAML."""
        items = get_value(yaml_data, editable.yaml_path) or []
        for index in range(len(items)):
            for child in (editable.children or []):
                if child.readonly or (child.readonly_on_edit and edit_mode):
                    continue
                concrete_path = resolve_path(child.yaml_path, index)
                value = parsed.get(concrete_path)
                if value is not None:
                    if child.converter:
                        value = child.converter.write(value)
                    set_value(yaml_data, concrete_path, value)
```

---

### Step 4: Rewrite Project Form Routes ✅ DONE

**Modify:** `opi/web/router_project_form.py`

Replace the entire content. Keep the existing patterns for auth, project lookup, and file save.

```python
"""Web routes for project form editing using editable-driven forms."""

import copy
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.project_registry import (
    get_all_project_editables,
    get_project_form_layout,
)
from opi.forms.editables.processor import EditableFormProcessor
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

project_form_router = APIRouter(prefix="/projects", tags=["project-forms"])


def create_form_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


@project_form_router.get("/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def edit_project_form(request: Request, project_name: str) -> HTMLResponse:
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    templates = get_templates()
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    project_data = project.data
    if not project_data:
        raise HTTPException(status_code=500, detail="Project data niet beschikbaar")

    renderer = create_form_renderer()
    editables = get_all_project_editables()
    layout = get_project_form_layout()

    form_html = renderer.render_from_editables(
        editables=editables,
        yaml_data=project_data,
        layout=layout,
        edit_mode=True,
        action=f"/projects/edit/{project_name}",
    )

    return templates.TemplateResponse(
        "project-edit-form.html.j2",
        {
            "request": request,
            "title": f"Bewerk Project - {project_data.get('display-name', project_name)}",
            "menu_items": get_menu_items(user),
            "project_name": project_name,
            "project_data": project_data,
            "form_html": form_html,
            "user": user,
        },
    )


@project_form_router.post("/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def save_project_form(request: Request, project_name: str) -> HTMLResponse:
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="Alleen admins kunnen projecten bewerken")

    form_data = await request.form()
    editables = get_all_project_editables()
    processor = EditableFormProcessor()

    # Parse form data
    parsed = processor.parse_form_data(form_data, editables)

    # Validate
    original_data = project.data or {}
    errors = processor.validate_editables(parsed, editables, original_data)

    if errors:
        # Re-render form with errors
        renderer = create_form_renderer()
        layout = get_project_form_layout()
        form_html = renderer.render_from_editables(
            editables=editables,
            yaml_data=original_data,
            layout=layout,
            errors=errors,
            edit_mode=True,
            action=f"/projects/edit/{project_name}",
        )
        templates = get_templates()
        return templates.TemplateResponse(
            "project-edit-form.html.j2",
            {
                "request": request,
                "title": f"Bewerk Project - {original_data.get('display-name', project_name)}",
                "menu_items": get_menu_items(user),
                "project_name": project_name,
                "project_data": original_data,
                "form_html": form_html,
                "errors": errors,
                "user": user,
            },
        )

    # Apply changes to YAML (deep-copies internally, preserves encrypted fields)
    updated_data = processor.apply_to_yaml(parsed, editables, original_data, edit_mode=True)

    # Save to file
    save_project_file(project.filename, updated_data)

    # Update in-memory cache
    project_service.load_project_from_data(updated_data, project.filename)

    logger.info(f"Project {project_name} updated by {user_email}")

    return RedirectResponse(
        url=f"/projects/details/{project_name}",
        status_code=302,
    )


def save_project_file(file_path: str, data: dict) -> None:
    """Save project data to a YAML file preserving formatting."""
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    with open(file_path, "w") as f:
        yaml.dump(data, f)
```

### Step 5: Wire Router Into App ✅ DONE

**Modify:** `opi/web/router.py`

Add after line 34 (`web_router.include_router(services_router)`):

```python
from .router_project_form import project_form_router
web_router.include_router(project_form_router)
```

### Step 6: Update Template ✅ DONE

**Modify:** `opi/templates/project-edit-form.html.j2`

The existing template is already close. Only needs:
- Global error display for enforcer errors
- The `form_html` rendering is already correct (`{{ form_html | process_components }}`)

```jinja2
{% extends "base.html.j2" %}

{% block page_title %}{{ title }}{% endblock %}

{% block content %}
<c-layout-flow gap="lg" size="lg">
    {# Breadcrumb #}
    <nav aria-label="Breadcrumb">
        <ol class="rvo-breadcrumb">
            <li><a href="/projects">Projecten</a></li>
            <li><a href="/projects/details/{{ project_name }}">{{ project_data.get('display-name', project_name) }}</a></li>
            <li aria-current="page">Bewerken</li>
        </ol>
    </nav>

    <c-heading type="h1" textContent="{{ title }}" />

    {% if errors %}
    <c-alert kind="warning" heading="Validatiefouten">
        <ul>
            {% for field, messages in errors.items() %}
            <li><strong>{{ field }}</strong>: {{ messages | join(', ') }}</li>
            {% endfor %}
        </ul>
    </c-alert>
    {% endif %}

    {% if global_errors %}
    <c-alert kind="error" heading="Fouten">
        <ul>
            {% for message in global_errors %}
            <li>{{ message }}</li>
            {% endfor %}
        </ul>
    </c-alert>
    {% endif %}

    {# Form Container #}
    <div class="project-form-container">
        {{ form_html | process_components }}
    </div>
</c-layout-flow>

<style>
    .rvo-breadcrumb {
        display: flex;
        list-style: none;
        padding: 0;
        margin: 0;
        gap: 0.5rem;
    }
    .rvo-breadcrumb li:not(:last-child)::after {
        content: "/";
        margin-left: 0.5rem;
        color: #666;
    }
    .rvo-breadcrumb a {
        color: #007BC7;
        text-decoration: none;
    }
    .rvo-breadcrumb a:hover {
        text-decoration: underline;
    }
    .project-form-container {
        background: white;
        padding: 2rem;
        border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    }
</style>
{% endblock %}
```

### Step 7: Tests ✅ DONE

**New file:** `tests/test_project_registry.py`
```python
# Test that all editables are defined, layout references valid fields,
# get_all_project_editables() returns expected count,
# sequence editables have children defined.
```

**New file:** `tests/test_editable_rendering.py`
```python
# Test render_from_editables() with sample YAML data produces HTML.
# Test sequence rendering (users with 2 items, components with 1 item).
# Test readonly fields render as disabled.
# Test display_card fields render with EncryptedDisplayConverter output.
# Test conditional visibility (should_render_editable skips fields).
```

**New file:** `tests/test_editable_processor.py`
```python
# Test parse_form_data with flat form keys → correct dict.
# Test validate_editables returns errors for invalid email.
# Test validate_editables returns empty dict for valid data.
# Test apply_to_yaml writes values to correct YAML paths.
# Test apply_to_yaml skips readonly fields.
# Test apply_to_yaml preserves encrypted fields untouched.
# Test converter.write() is called before set_value().
```

---

## Files Summary

| Action | File | Purpose |
|--------|------|---------|
| **Create** | `opi/forms/editables/project_registry.py` | All ProjectEditable instances, layout, parts |
| **Create** | `opi/forms/editables/processor.py` | Form data parsing, validation, YAML write-back |
| **Modify** | `opi/forms/renderer.py` | Add `render_from_editables()`, `_build_fields_from_editables()`, `_build_sequence_field()` |
| **Modify** | `opi/web/router_project_form.py` | Rewrite edit routes to use editables |
| **Modify** | `opi/web/router.py` | One-liner: wire in `project_form_router` |
| **Modify** | `opi/templates/project-edit-form.html.j2` | Add global_errors display |
| **Create** | `tests/test_project_registry.py` | Registry definition tests |
| **Create** | `tests/test_editable_rendering.py` | Rendering pipeline tests |
| **Create** | `tests/test_editable_processor.py` | Processor (save) tests |

---

## Verification

1. **Unit tests pass:** `cd operations-manager/python && pytest tests/test_project_registry.py tests/test_editable_rendering.py tests/test_editable_processor.py -v`
2. **Existing tests still pass:** `pytest` (no regressions)
3. **Linting clean:** `ruff check . --fix && ruff format . && pyright`
4. **Manual verification:** Load an existing project YAML from `/Users/robbertuittenbroek/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects/`, visit `/projects/edit/{project_name}`, verify:
   - Form renders with correct values from YAML
   - Identity fields are readonly in edit mode (name)
   - Sequence fields (users, components, deployments) show correct items
   - Service cards show selected services
   - Config section shows display cards (encrypted/truncated)
   - Submit form, verify YAML file is updated
   - Encrypted fields preserved bit-for-bit after save

---

## Known Complexity: Nested Sequences

Deployments contain a nested sequence: `deployments[*]/components[*]`. The `resolve_path()` function replaces only the first `[*]`. For double-nested paths like `deployments[*]/components[*]/reference`, the implementation must:

1. First resolve parent index: `deployments[0]/components[*]/reference`
2. Then resolve child index: `deployments[0]/components[0]/reference`

**Recommended approach:** When building nested sequence FormFields in `_build_nested_sequence_field()`, create temporary copies of child editables with `yaml_path` pre-resolved for the parent index before passing to `editable_to_form_field()` with the child index. This avoids modifying `resolve_path()` itself.

---

## Out of Scope (For Later)

- **Create form** (project creation with auto-generation of AGE keys, repos, etc.)
- **Wizard/tab UI** (splitting the form into steps)
- **HTMX dynamic updates** (service config sub-forms loaded on toggle)
- **Service config sub-forms** (keycloak template selection, PostgreSQL instances/storage)
- **Drag-to-reorder** for sequence items
- **Cross-part option filtering** at runtime (components showing only project-enabled services)

BUILD_COMPLETE_MARKER
