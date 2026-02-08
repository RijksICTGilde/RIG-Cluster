# 03 - Services Part

## Overview

The Services part is the most complex UX challenge. Users select which infrastructure services their project needs. Some services are simple toggles, while others require additional configuration. When a configurable service is selected, a dynamic sub-form appears via HTMX. This tests field dependencies at the HTMX level.

## YAML Structure

```yaml
services:
  - publish-on-web                    # Simple toggle → string
  - persistent-storage                # Simple toggle → string
  - keycloak:                         # Configurable → dict
      config:
        template: sso-support
        additional_redirect_uris:
          - http://localhost:8080/*
  - namespace-postgresql-database:    # Configurable → dict
      config:
        instances: 1
        storage: 1Gi
```

The mixed string/dict format is a key challenge. `ServiceListConverter` handles this.

## Editable Definitions

### Main service selection

```python
class ProjectEditables:

    # === Services ===

    SERVICES = ProjectEditable(
        yaml_path="services",
        widget="service-cards",
        label="project.services",
        description="project.services.description",
        converter=ServiceListConverter(),
        # Options are driven by ServiceAdapter.SERVICE_DEFINITIONS
        # The service-cards widget uses ServiceOptionsProvider internally
        options_provider="ServiceOptionsProvider",
    )
```

### Keycloak config sub-form editables

These editables are rendered **only when keycloak is selected**, loaded via HTMX.

```python
    KEYCLOAK_TEMPLATE = ProjectEditable(
        yaml_path="services/keycloak/config/template",
        widget="select",
        label="service.keycloak.template",
        description="service.keycloak.template.description",
        options_provider="KeycloakTemplateOptionsProvider",
        depends_on="services",
        show_when={"contains": "keycloak"},
    )

    KEYCLOAK_REDIRECT_URIS = ProjectEditable(
        yaml_path="services/keycloak/config/additional_redirect_uris",
        widget="textarea",
        label="service.keycloak.redirect_uris",
        description="service.keycloak.redirect_uris.description",
        placeholder="http://localhost:8080/*\nhttp://127.0.0.1:8080/*",
        converter=NewlineSeparatedListConverter(),
        depends_on="services",
        show_when={"contains": "keycloak"},
    )
```

### PostgreSQL config sub-form editables

```python
    POSTGRESQL_INSTANCES = ProjectEditable(
        yaml_path="services/namespace-postgresql-database/config/instances",
        widget="number",
        label="service.postgresql.instances",
        description="service.postgresql.instances.description",
        validator=RangeValidator(min_value=1, max_value=3),
        depends_on="services",
        show_when={"contains": "namespace-postgresql-database"},
    )

    POSTGRESQL_STORAGE = ProjectEditable(
        yaml_path="services/namespace-postgresql-database/config/storage",
        widget="select",
        label="service.postgresql.storage",
        description="service.postgresql.storage.description",
        options_provider="StorageSizeOptionsProvider",
        depends_on="services",
        show_when={"contains": "namespace-postgresql-database"},
    )
```

## How `service-cards` renders each service

The `service-cards` widget renders one card per option from `ServiceOptionsProvider`. Each option includes metadata from `ServiceAdapter.SERVICE_DEFINITIONS`:

| Service | Widget rendering | Icon | Color |
|---------|-----------------|------|-------|
| `publish-on-web` | Card with checkbox, no sub-form | globe | hemelblauw |
| `persistent-storage` | Card with checkbox, no sub-form | hard-drive | groen |
| `temp-storage` | Card with checkbox, no sub-form | folder | grijs |
| `redis` | Card with checkbox, no sub-form | database | rood |
| `keycloak` | Card with checkbox + HTMX sub-form trigger | shield | oranje |
| `namespace-postgresql-database` | Card with checkbox + HTMX sub-form trigger | database | paars |
| `minio-storage` | Card with checkbox, no sub-form (future: config) | cloud | hemelblauw |

### HTMX dependency: service card → config sub-form

When a configurable service card checkbox is toggled:

```html
<!-- Service card for keycloak -->
<div class="service-card">
    <c-checkbox
        name="services[]"
        value="keycloak"
        hx-get="/projects/parts/services/config/keycloak"
        hx-target="#service-config-keycloak"
        hx-swap="innerHTML"
        hx-trigger="change[this.checked]"
    />
    <!-- ... card content ... -->
</div>
<!-- Container for config sub-form -->
<div id="service-config-keycloak"></div>
```

When **unchecked**, a separate HTMX trigger clears the sub-form:

```html
<c-checkbox
    ...
    hx-get="/projects/parts/services/config/empty"
    hx-target="#service-config-keycloak"
    hx-swap="innerHTML"
    hx-trigger="change[!this.checked]"
/>
```

The config sub-form route renders the keycloak config editables:
```
GET /projects/parts/services/config/keycloak

1. Determine which editables belong to keycloak config:
   [KEYCLOAK_TEMPLATE, KEYCLOAK_REDIRECT_URIS]
2. For each: editable_to_form_field() with current yaml_data (or defaults)
3. Render using a Fieldset layout
4. Return HTML fragment
```

## Converter

### ServiceListConverter

Handles the mixed string/dict YAML format:

```python
class ServiceListConverter:
    """Converts between YAML services list and form representation."""

    def read(self, value: Any) -> dict:
        """
        Convert YAML services list to form-friendly format.

        Input: ["publish-on-web", {"keycloak": {"config": {"template": "sso-support"}}}]
        Output: {
            "selected": ["publish-on-web", "keycloak"],
            "configs": {"keycloak": {"template": "sso-support"}}
        }
        """
        if not isinstance(value, list):
            return {"selected": [], "configs": {}}

        selected = []
        configs = {}
        for item in value:
            if isinstance(item, str):
                selected.append(item)
            elif isinstance(item, dict):
                name = next(iter(item.keys()))
                selected.append(name)
                config = item[name]
                if isinstance(config, dict) and "config" in config:
                    configs[name] = config["config"]
                elif isinstance(config, dict):
                    configs[name] = config
        return {"selected": selected, "configs": configs}

    def write(self, value: Any) -> list:
        """
        Convert form data back to YAML services list format.

        Input: {"selected": ["publish-on-web", "keycloak"], "configs": {"keycloak": {"template": "sso-support"}}}
        Output: ["publish-on-web", {"keycloak": {"config": {"template": "sso-support"}}}]
        """
        if not isinstance(value, dict):
            return []

        selected = value.get("selected", [])
        configs = value.get("configs", {})

        result = []
        for name in selected:
            if name in configs and configs[name]:
                result.append({name: {"config": configs[name]}})
            else:
                result.append(name)
        return result

    def view(self, value: Any) -> list[str]:
        """For display: just return the service names."""
        parsed = self.read(value)
        return parsed.get("selected", [])
```

### NewlineSeparatedListConverter

```python
class NewlineSeparatedListConverter:
    """Converts between list and newline-separated string."""

    def read(self, value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(str(v) for v in value)
        return str(value) if value else ""

    def write(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [v.strip() for v in value.split("\n") if v.strip()]
        if isinstance(value, list):
            return value
        return []

    def view(self, value: Any) -> str:
        return self.read(value)
```

## Part Definition

```python
class ProjectParts:

    SERVICES = EditablePart(
        part_id="services",
        title="Services",
        icon="puzzelstuk",
        description="Selecteer de services die uw project nodig heeft",
        editables=[
            ProjectEditables.SERVICES,
            # Config sub-form editables (rendered conditionally via HTMX):
            ProjectEditables.KEYCLOAK_TEMPLATE,
            ProjectEditables.KEYCLOAK_REDIRECT_URIS,
            ProjectEditables.POSTGRESQL_INSTANCES,
            ProjectEditables.POSTGRESQL_STORAGE,
        ],
        layout=Fieldset(
            legend="project.services.title",
            description="project.services.description",
            children=[
                "services",  # service-cards widget
                Div(
                    css_class="service-configs",
                    attributes={"id": "service-configs-container"},
                    children=[],  # Config sub-forms loaded via HTMX
                ),
            ],
        ),
        in_create_wizard=True,
        wizard_step=2,
        summary_fn=services_summary,
    )
```

## Providers Needed

### KeycloakTemplateOptionsProvider

```python
class KeycloakTemplateOptionsProvider:
    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "sso-support", "label": "SSO Support (standaard)",
             "description": "Inloggen via SSO, applicatie beheert eigen autorisatie"},
            {"value": "sso-only", "label": "SSO Only",
             "description": "Alleen SSO-login, geen eigen autorisatie"},
            {"value": "algoritmeregister", "label": "Algoritmeregister",
             "description": "Specifiek voor algoritmeregister applicaties"},
        ]
```

### StorageSizeOptionsProvider

```python
class StorageSizeOptionsProvider:
    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "1Gi", "label": "1 GB"},
            {"value": "5Gi", "label": "5 GB"},
            {"value": "10Gi", "label": "10 GB"},
            {"value": "25Gi", "label": "25 GB"},
            {"value": "50Gi", "label": "50 GB"},
        ]
```

## UX Behavior

### Create wizard (Step 2)
- Service cards shown in a responsive grid (2-3 columns)
- Each card: icon + name + short description + checkbox toggle
- Clicking a card toggles the service
- Configurable services: checking the card triggers HTMX GET to load config sub-form below
- Sub-forms have sensible defaults pre-filled
- User can proceed without selecting any services

### Edit tab ("Services")
- Same card grid, but current selections are pre-checked
- Existing config loaded into sub-forms
- Removing a service: confirmation "Weet u zeker dat u {service} wilt uitschakelen?"
- Adding a service: config sub-form loaded with defaults

### Progressive disclosure (dependency-driven)
- Simple services (publish-on-web, redis, etc.) → just the card
- Configurable services (keycloak, postgresql) → card + expandable config panel
- The config panel is **only loaded via HTMX** when the card is checked
- This is a Level 2 dependency (HTMX-driven dynamic visibility)

## Display Summary

```python
def services_summary(data: dict) -> str:
    services = get_value(data, "services") or []
    # Extract names from mixed format
    names = []
    for svc in services:
        if isinstance(svc, str):
            names.append(svc)
        elif isinstance(svc, dict):
            names.append(next(iter(svc.keys())))
    if not names:
        return "Geen services geselecteerd"
    return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
```

## Validation Rules

1. No duplicate services
2. If keycloak is selected, template is required (enforced by `KEYCLOAK_TEMPLATE.required`)
3. If postgresql is selected, instances must be 1-3, storage must be valid
4. Service config sub-forms validate independently when submitted

## Acceptance Criteria

- [ ] Service cards render in responsive grid with correct icons/colors from ServiceAdapter
- [ ] Toggling a simple service adds/removes the string from the YAML services list
- [ ] Toggling keycloak triggers HTMX GET to load config sub-form
- [ ] Deselecting keycloak clears the config sub-form via HTMX
- [ ] Keycloak config sub-form shows template select + redirect URIs textarea
- [ ] PostgreSQL config sub-form shows instances number + storage select
- [ ] ServiceListConverter round-trip: load YAML → render → save → YAML produces equivalent output
- [ ] Config sub-form defaults are sensible (sso-support, 1 instance, 1Gi)
- [ ] Summary shows selected service names
- [ ] `depends_on` / `show_when` correctly gates config editables
