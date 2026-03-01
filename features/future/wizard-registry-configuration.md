# Wizard: Registry Configuration Step

## Problem

Container registries are configured by manually editing the project YAML. There's no wizard step for adding registries, which means users need to understand the YAML structure to use private images.

## Proposal

Add a `REGISTRIES_SECTION` as a dedicated wizard step for managing container registries. This should feel similar to the services configuration but is a project-level setting, not a service.

### Wizard placement

Between `COMPONENTS_SECTION` and `DOMAIN_SECTION` — close to where images are configured on components. Could also be conditional: only shown when at least one component has a non-public image.

### Form fields per registry entry

| Field | Type | Description |
|-------|------|-------------|
| `name` | Text | Unique identifier for the registry |
| `url` | Text | Registry URL (may include path, e.g., `rcr.rijksapps.nl/rig`) |
| Auth mode | Radio | "Credentials" or "Existing secret" |
| `username` | Text | Only if credentials mode |
| `password` | Text/Secret | Only if credentials mode (AGE-encrypted on save) |
| `secretName` | Text | Only if existing secret mode |

### Implementation sketch

- **Editable**: `SEQUENCE` at `yaml_path="registries"` with children for each field
- **Section**: `FormSection(section_id="registries", ...)` in `wizard_sections.py`
- **Flow**: Insert into `CREATE_FLOW` and `EDIT_FLOW` in `flows.py`
- **Converter**: Needs a custom converter to handle the auth mode toggle (credentials vs secretName)
- **Validation**: Registry name uniqueness, URL format, either credentials or secretName required

### Component linking

Once registries are defined, the component editor (step 7) should offer a dropdown to select a registry for each component. Currently this is a free-text `registry` field — it should become a select populated from the registries defined in the previous step.

### Key files

- `opi/forms/editables/fields/services.py` — new editable definitions
- `opi/forms/visualizers/fields/services.py` — new visualizers
- `opi/forms/visualizers/wizard_sections.py` — new section
- `opi/forms/visualizers/flows.py` — insert section into flows

### Dependencies

- Relies on `secretName` support in registry config (implemented)
- Relies on the editable-driven forms system
