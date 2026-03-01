# 01 - Project Identity Part

## Overview

The Project Identity part captures the core project metadata: its technical name, human-readable display name, description, and target cluster(s). This is the simplest editable part and should be built first to prove the editable architecture end-to-end.

## YAML Structure

```yaml
name: my-project-x7k
display-name: Mijn Applicatie
description: Een beschrijving van het project
clusters:
  - local
  - odcn-production
```

## Editable Definitions

```python
# In opi/forms/editables/registry.py

class ProjectEditables:
    """Declarative registry of all project YAML form fields."""

    # === Identity ===

    NAME = ProjectEditable(
        yaml_path="name",
        widget="text",
        label="project.name",
        description="project.name.description",
        placeholder="mijn-project",
        readonly_on_edit=True,
        required=True,
        validator=SlugValidator(),
    )

    DISPLAY_NAME = ProjectEditable(
        yaml_path="display-name",
        widget="text",
        label="project.display_name",
        description="project.display_name.description",
        placeholder="Mijn Applicatie",
        required=True,
        validator=MinMaxLengthValidator(3, 100),
    )

    DESCRIPTION = ProjectEditable(
        yaml_path="description",
        widget="textarea",
        label="project.description",
        description="project.description.description",
        placeholder="Beschrijf het doel van uw project...",
    )

    CLUSTERS = ProjectEditable(
        yaml_path="clusters",
        widget="checkbox-group",
        label="project.clusters",
        description="project.clusters.description",
        options_provider="ClusterOptionsProvider",
        required=True,
        min_items=1,
    )
```

## Part Definition

```python
class ProjectParts:

    IDENTITY = EditablePart(
        part_id="identity",
        title="Uw project",
        icon="huis",
        description="Basisgegevens van uw project",
        editables=[
            ProjectEditables.NAME,
            ProjectEditables.DISPLAY_NAME,
            ProjectEditables.DESCRIPTION,
            ProjectEditables.CLUSTERS,
        ],
        layout=Fieldset(
            legend="project.identity.title",
            description="project.identity.description",
            children=[
                Row(children=[
                    Column("name", width=4),
                    Column("display-name", width=8),
                ]),
                "description",
                "clusters",
            ],
        ),
        in_create_wizard=True,
        wizard_step=1,
        summary_fn=identity_summary,
    )
```

Note: In the layout, field references use the `yaml_path` string (e.g., `"name"`, `"display-name"`). The bridge function maps `editable.yaml_path` to the `FormField.name`, so layout field references match.

## Render Flow

```
GET /projects/{name}/parts/identity

1. Load project YAML dict
2. For each editable in IDENTITY.editables:
   - get_value(yaml, "name")           → "my-project-x7k"
   - get_value(yaml, "display-name")   → "Mijn Applicatie"
   - get_value(yaml, "description")    → "Een beschrijving..."
   - get_value(yaml, "clusters")       → ["local", "odcn-production"]
3. editable_to_form_field() for each → 4 FormField instances
4. Render via ROOSWidgetAdapter using IDENTITY.layout
5. Wrap in tab_panel.html.j2
```

## Save Flow

```
POST /projects/{name}/parts/identity

1. Parse form data: {"display-name": "Nieuwe Naam", "description": "...", "clusters": ["local"]}
2. For each non-readonly editable:
   - DISPLAY_NAME.validator.validate("Nieuwe Naam") → [] (valid)
   - CLUSTERS.required check → ["local"] has items → valid
3. set_value(yaml, "display-name", "Nieuwe Naam")
   set_value(yaml, "description", "...")
   set_value(yaml, "clusters", ["local"])
4. Write YAML, commit to git
5. Return updated tab_panel.html.j2 with success alert
```

## Validators

### SlugValidator

```python
class SlugValidator:
    """Validates slug format: lowercase letters, digits, hyphens."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        if not re.match(r"^[a-z][a-z0-9-]*$", str(value)):
            return ["Naam mag alleen kleine letters, cijfers en streepjes bevatten, en moet beginnen met een letter"]
        return []
```

### MinMaxLengthValidator

```python
class MinMaxLengthValidator:
    """Validates string length within bounds."""

    def __init__(self, min_length: int = 0, max_length: int | None = None):
        self.min_length = min_length
        self.max_length = max_length

    def validate(self, value: Any) -> list[str]:
        if value is None:
            return []
        length = len(str(value))
        if length < self.min_length:
            return [f"Minimaal {self.min_length} tekens vereist"]
        if self.max_length and length > self.max_length:
            return [f"Maximaal {self.max_length} tekens toegestaan"]
        return []
```

## UX Behavior

### Create wizard (Step 1)
- `name` field is hidden; auto-generated from `display-name` by a `SlugEnforcerHook` in the wizard step handler
- A random 3-character suffix is appended to ensure uniqueness
- The generated name is shown as a preview below the display-name field: "Technische naam: `mijn-applicatie-x7k`"
- `clusters` defaults to "local" pre-selected

### Edit tab ("Algemeen")
- `name` shown as read-only (greyed out, `readonly_on_edit=True`)
- `display-name`, `description`, `clusters` all editable
- "Opslaan" button below the form saves via HTMX POST
- Success: green `c-alert` "Projectgegevens opgeslagen"
- Validation error: red `c-alert` with error list + inline field errors

## Display Summary

```python
def identity_summary(data: dict) -> str:
    name = get_value(data, "display-name") or get_value(data, "name") or ""
    clusters = get_value(data, "clusters") or []
    return f"{name} ({len(clusters)} cluster{'s' if len(clusters) != 1 else ''})"
```

## Validation Rules

1. `display-name` is required, 3-100 characters
2. `clusters` must have at least one selection
3. `name` must match pattern `^[a-z][a-z0-9-]*$`
4. On create: `name` must be unique across all projects (checked via wizard handler, not the editable validator)

## Acceptance Criteria

- [ ] Identity part renders correctly in both wizard step and edit tab
- [ ] On create, name is auto-generated from display-name with random suffix
- [ ] On edit, name field is read-only (greyed out)
- [ ] Clusters show all available options from ClusterOptionsProvider
- [ ] HTMX save works: POST saves to YAML, returns updated HTML
- [ ] Validation errors show inline and as summary alert
- [ ] `get_value()` / `set_value()` correctly handle `display-name` (hyphenated key)
- [ ] Display summary shows project name and cluster count
