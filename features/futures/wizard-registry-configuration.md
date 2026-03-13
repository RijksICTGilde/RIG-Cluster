# Wizard: Registry Configuration Step

**Status**: Planned
**Priority**: Medium
**Created**: 2026-02-10

## Problem

Container registries are configured by manually editing the project YAML. There's no wizard step for adding registries, which means users need to understand the YAML structure to use private images.

## Current YAML Structure

```yaml
registries:
  - name: my-registry
    url: rcr.rijksapps.nl/rig
    username: deploy-user
    password: ENC[AGE,...]     # AGE-encrypted
  - name: ghcr
    url: ghcr.io
    secretName: ghcr-pull-secret   # Pre-existing Kubernetes secret
```

## Proposal

Add a `REGISTRIES_SECTION` as a dedicated wizard step for managing container registries.

### Wizard Placement

Between `COMPONENTS_SECTION` and `DOMAIN_SECTION` — close to where images are configured on components. Shown in both `CREATE_FLOW` and `EDIT_FLOW`.

---

## Implementation

### Phase 1: Editable Definitions

**File**: `opi/forms/editables/fields/registries.py` (new)

```python
from opi.forms.editables.base import Editable, EditableType

REGISTRY_NAME = Editable(
    name="name",
    yaml_path="registries[*].name",
    editable_type=EditableType.TEXT,
    label="Naam",
    help_text="Unieke naam voor deze registry (bijv. 'ghcr', 'harbor')",
    required=True,
    validators=["unique_in_sequence", "dns_label"],
    placeholder="my-registry",
)

REGISTRY_URL = Editable(
    name="url",
    yaml_path="registries[*].url",
    editable_type=EditableType.TEXT,
    label="Registry URL",
    help_text="Volledige URL inclusief pad (bijv. 'rcr.rijksapps.nl/rig')",
    required=True,
    validators=["registry_url"],
    placeholder="rcr.rijksapps.nl/rig",
)

REGISTRY_AUTH_MODE = Editable(
    name="auth-mode",
    yaml_path="registries[*].auth-mode",
    editable_type=EditableType.RADIO,
    label="Authenticatie",
    options=[
        {"value": "credentials", "label": "Gebruikersnaam & wachtwoord"},
        {"value": "secret", "label": "Bestaand Kubernetes secret"},
    ],
    default="credentials",
    required=True,
)

REGISTRY_USERNAME = Editable(
    name="username",
    yaml_path="registries[*].username",
    editable_type=EditableType.TEXT,
    label="Gebruikersnaam",
    required=True,
    visible_when={"auth-mode": "credentials"},
    placeholder="deploy-user",
)

REGISTRY_PASSWORD = Editable(
    name="password",
    yaml_path="registries[*].password",
    editable_type=EditableType.SECRET,
    label="Wachtwoord / Token",
    help_text="Wordt versleuteld opgeslagen met AGE-encryptie",
    required=True,
    visible_when={"auth-mode": "credentials"},
)

REGISTRY_SECRET_NAME = Editable(
    name="secretName",
    yaml_path="registries[*].secretName",
    editable_type=EditableType.TEXT,
    label="Secret naam",
    help_text="Naam van een bestaand Kubernetes secret met registry credentials",
    required=True,
    visible_when={"auth-mode": "secret"},
    placeholder="ghcr-pull-secret",
    validators=["dns_label"],
)
```

### Phase 2: Registry Converter

**File**: `opi/forms/converters/registry_converter.py` (new)

Handles the auth mode toggle — converts between the two credential formats:

```python
from opi.forms.converters.base import Converter


class RegistryConverter(Converter):
    """
    Converts between form representation (with auth-mode radio)
    and YAML representation (credentials vs secretName).
    """

    def form_to_yaml(self, form_data: dict) -> dict:
        """Convert form submission to YAML-ready dict."""
        result = {
            "name": form_data["name"],
            "url": form_data["url"],
        }

        auth_mode = form_data.get("auth-mode", "credentials")
        if auth_mode == "credentials":
            result["username"] = form_data["username"]
            result["password"] = form_data["password"]  # Will be AGE-encrypted by save logic
        elif auth_mode == "secret":
            result["secretName"] = form_data["secretName"]

        # Don't persist auth-mode itself — it's a UI-only field
        return result

    def yaml_to_form(self, yaml_data: dict) -> dict:
        """Convert YAML data to form-ready dict."""
        result = {
            "name": yaml_data.get("name", ""),
            "url": yaml_data.get("url", ""),
        }

        if "secretName" in yaml_data:
            result["auth-mode"] = "secret"
            result["secretName"] = yaml_data["secretName"]
        else:
            result["auth-mode"] = "credentials"
            result["username"] = yaml_data.get("username", "")
            result["password"] = yaml_data.get("password", "")  # Shows as "encrypted" in form

        return result
```

### Phase 3: Validation

**File**: `opi/forms/validators/registry_validators.py` (new)

```python
import re


def validate_registry_url(value: str) -> tuple[bool, str]:
    """Validate container registry URL format."""
    # Must be a valid hostname, optionally with port and path
    pattern = r'^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d+)?(/[a-z0-9._/-]*)?$'
    if not re.match(pattern, value):
        return False, "Ongeldige registry URL. Verwacht formaat: hostname/pad (bijv. 'rcr.rijksapps.nl/rig')"
    return True, ""


def validate_registry_name_unique(value: str, all_registries: list[dict]) -> tuple[bool, str]:
    """Ensure registry name is unique within the project."""
    names = [r.get("name") for r in all_registries]
    if names.count(value) > 1:
        return False, f"Registry naam '{value}' komt meer dan eens voor"
    return True, ""
```

### Phase 4: Wizard Section

**File**: `opi/forms/visualizers/wizard_sections.py` (modify)

Add the new section:

```python
from opi.forms.editables.fields.registries import (
    REGISTRY_NAME, REGISTRY_URL, REGISTRY_AUTH_MODE,
    REGISTRY_USERNAME, REGISTRY_PASSWORD, REGISTRY_SECRET_NAME,
)
from opi.forms.converters.registry_converter import RegistryConverter

REGISTRIES_SECTION = FormSection(
    section_id="registries",
    title="Container registries",
    icon="box",
    description="Configureer private container registries voor het ophalen van images",
    editables=[
        REGISTRY_NAME,
        REGISTRY_URL,
        REGISTRY_AUTH_MODE,
        REGISTRY_USERNAME,
        REGISTRY_PASSWORD,
        REGISTRY_SECRET_NAME,
    ],
    layout=[
        Sequence(
            field_name="registries",
            add_label="Registry toevoegen",
            remove_label="Verwijderen",
            converter=RegistryConverter(),
            child_layout=[
                Fieldset(legend="Registry", children=[
                    "name",
                    "url",
                ]),
                Fieldset(legend="Authenticatie", children=[
                    "auth-mode",
                    "username",
                    "password",
                    "secretName",
                ]),
            ],
        ),
    ],
    optional=True,  # Section can be empty (no registries = public images only)
)
```

### Phase 5: Flow Integration

**File**: `opi/forms/visualizers/flows.py` (modify)

Insert the registries section into both flows:

```python
CREATE_FLOW = FormFlow(
    flow_id="create-project",
    title="Nieuw project aanmaken",
    mode=FlowMode.WIZARD,
    sections=[
        IDENTITY_SECTION,
        SERVICES_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        AUTH_WALL_SECTION,
        TEAM_SECTION,
        COMPONENTS_SECTION,
        REGISTRIES_SECTION,     # <-- NEW: between components and domain
        DOMAIN_SECTION,
        DEPLOYMENT_SECTION,
    ],
)

EDIT_FLOW = FormFlow(
    flow_id="edit-project",
    title="Project bewerken",
    mode=FlowMode.TABS,
    sections=[
        IDENTITY_SECTION,
        SERVICES_SECTION,
        KEYCLOAK_CONFIG_SECTION,
        POSTGRESQL_CONFIG_SECTION,
        AUTH_WALL_SECTION,
        TEAM_SECTION,
        COMPONENTS_SECTION,
        REGISTRIES_SECTION,     # <-- NEW
        DOMAIN_SECTION,
        DEPLOYMENTS_SECTION,
    ],
)
```

### Phase 6: Component Registry Dropdown

Once registries are defined, the component editor should offer a dropdown to select a registry for each component instead of a free-text field.

**File**: `opi/forms/editables/fields/components.py` (modify)

```python
# Change the registry field from free-text to select:
COMPONENT_REGISTRY = Editable(
    name="registry",
    yaml_path="components[*].registry",
    editable_type=EditableType.SELECT,
    label="Registry",
    help_text="Selecteer de registry voor dit component's image",
    options_from="registries",   # Populated from registries defined in the project
    options_value_field="name",
    options_label_field="name",
    allow_empty=True,            # Empty = public registry (Docker Hub)
    empty_label="Publiek (Docker Hub)",
)
```

### Phase 7: AGE Encryption on Save

**File**: `opi/forms/save_handler.py` (modify)

Ensure the password field is AGE-encrypted when saving:

```python
async def save_registries(self, project_data: dict, form_data: dict) -> dict:
    """Process registry form data, encrypting passwords."""
    registries = form_data.get("registries", [])

    for registry in registries:
        if "password" in registry and not registry["password"].startswith("ENC[AGE,"):
            # Encrypt the password using the project's AGE public key
            encrypted = await self.encrypt_value(
                registry["password"],
                project_data.get("age-public-key"),
            )
            registry["password"] = encrypted

    project_data["registries"] = registries
    return project_data
```

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `opi/forms/editables/fields/registries.py` | Editable field definitions for registry form |
| `opi/forms/converters/registry_converter.py` | Auth mode toggle converter (credentials vs secretName) |
| `opi/forms/validators/registry_validators.py` | URL format + name uniqueness validators |

### Modified Files

| File | Change |
|------|--------|
| `opi/forms/visualizers/wizard_sections.py` | Add `REGISTRIES_SECTION` |
| `opi/forms/visualizers/flows.py` | Insert section into `CREATE_FLOW` and `EDIT_FLOW` |
| `opi/forms/editables/fields/components.py` | Change registry field from text to select |
| `opi/forms/save_handler.py` | AGE encryption for registry passwords |

---

## Dependencies

- Editable-driven forms system (implemented)
- `secretName` support in registry config (implemented)
- AGE encryption for sensitive fields (implemented via SOPS age key)

## Verification

1. **Create flow**: New project wizard shows registries step between components and domain
2. **Credentials mode**: Enter username/password, verify password is AGE-encrypted in saved YAML
3. **Secret mode**: Select "existing secret", verify only `secretName` is saved (no username/password)
4. **Validation**: Duplicate registry names show error; invalid URLs show error
5. **Component linking**: After defining registries, component registry field shows dropdown with defined registries
6. **Edit flow**: Open existing project with registries, verify form loads correctly with existing data
7. **Empty registries**: Skip the registries step entirely, verify project saves with no `registries` key
8. **Round-trip**: Create registry, edit project, verify registry data persists correctly
