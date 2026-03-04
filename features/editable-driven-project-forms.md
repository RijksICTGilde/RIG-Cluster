# Editable-Driven Project Forms

## What It Is

A dynamic form system for editing existing projects. Instead of hard-coded HTML templates, forms are generated from declarative `ProjectEditable` definitions that map directly to the project YAML structure. This makes adding, removing, or modifying form fields a matter of changing a Python definition rather than rewriting HTML and JavaScript.

## How to Use It

### Editing a Project

Navigate to:

```
GET /projects/edit/{project_name}
```

Where `{project_name}` is the technical project name (e.g., `amt-dev`, `hello-0o5`). This renders a form with all editable sections pre-filled from the project's YAML data.

The form submits to:

```
POST /projects/edit/{project_name}
```

On successful save, you are redirected to the project details page. Validation errors are displayed inline on the form.

### Authentication

Both routes require SSO authentication. The GET route requires project membership. The POST route requires `admin` or `owner` role on the project.

## Form Sections

The edit form contains these sections:

| Section | Fields | Editable? |
|---------|--------|-----------|
| **Projectgegevens** | Name (readonly), display name, description, clusters | Yes (name is readonly in edit mode) |
| **Projectleden** | Email + role per user (add/remove rows) | Yes |
| **Services** | Service cards (publish-on-web, keycloak, storage, database, etc.) | Yes |
| **Componenten** | Name, type, ports, CPU/memory, service bindings, aliases per component | Yes |
| **Deployments** | Name (readonly), cluster, repository, subdomain, component images per deployment | Partially (name readonly) |
| **Configuratie** | AGE keys, API key | Display-only |

Encrypted fields (AGE-encrypted passwords, private keys) are never shown or editable -- they are displayed as status cards showing "Versleuteld opgeslagen".

## Architecture

### Key Files

| File | Purpose |
|------|---------|
| `opi/forms/editables/project_registry.py` | All field definitions (what the form contains) |
| `opi/forms/editables/processor.py` | Form submission handling (parse, validate, save to YAML) |
| `opi/forms/renderer.py` | `render_from_editables()` method generates HTML from definitions |
| `opi/web/router_project_form.py` | FastAPI routes (GET edit, POST edit) |
| `opi/templates/project-edit-form.html.j2` | Jinja2 template wrapper |

### Data Flow

**Rendering (GET):**
```
Project YAML dict
  -> ProjectEditable definitions (project_registry.py)
  -> editable_to_form_field() bridge (bridge.py)
  -> FormField objects
  -> FormRenderer._render_layout_element() with layout
  -> ROOSWidgetAdapter renders HTML
  -> Template wraps in page
```

**Saving (POST):**
```
HTML form data
  -> EditableFormProcessor.parse_form_data()
  -> EditableFormProcessor.validate_editables()
  -> EditableFormProcessor.apply_to_yaml() (deep-copies, skips readonly, applies converters)
  -> save_project_file() writes YAML to disk
  -> ProjectService cache updated
```

### Adding a New Field

To add a field to the form, edit `project_registry.py`:

1. Define a new `ProjectEditable`:
```python
MY_FIELD = ProjectEditable(
    yaml_path="components[*]/my-field",
    widget="text",
    label="Mijn veld",
    description="Beschrijving van het veld",
)
```

2. Add it to the appropriate sequence's `children` list or to `get_all_project_editables()`
3. Add it to the layout in `get_project_form_layout()`

No template changes needed.

## Infrastructure (Reused)

The form system builds on the editables infrastructure:

- **Editables package** (`opi/forms/editables/`): Core dataclasses, YAML path utilities, bridge functions, converters, validators, enforcers
- **Widget adapter** (`opi/forms/widgets/roos.py`): ROOS component rendering for all widget types
- **Layout system** (`opi/forms/layout.py`): Row/Column/Fieldset/Sequence composition
- **Providers** (`opi/forms/providers.py`): Dynamic options for select/checkbox fields (clusters, services, component types, etc.)

## Testing

Infrastructure tests exist for the editables package:

```bash
cd operations-manager/python
pytest tests/test_editables_*.py -v
```

## Limitations (Current)

- **Edit only** -- no create-new-project form yet (the existing self-service portal handles creation)
- **Single page** -- all sections on one form (wizard/tab split planned for later)
- **No HTMX dynamics** -- service config sub-forms (keycloak template, PostgreSQL options) are not yet loaded dynamically
- **No cross-part filtering at runtime** -- component service checkboxes don't yet filter based on selected project services
