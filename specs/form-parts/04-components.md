# 04 - Components Part

## Overview

The Components part defines the deployable units of the application. Each component is a container with ports, resource limits, service dependencies, storage volumes, and environment variables. This is a complex nested sequence with **cross-part dependencies** (uses-services must reference project-level services) and **intra-field dependencies** (publish-on-web and sso-rijk flags depend on which services are enabled).

## YAML Structure

```yaml
components:
  - name: component-1
    type: single
    ports:
      inbound:
        - 8000
      outbound:
        - 80
        - 443
    path: /aanleverapi
    uses-services:
      - publish-on-web
      - keycloak
      - namespace-postgresql-database
    uses-components: []
    resources:
      cpu: '1'
      memory: 256Mi
    storage:
      - name: data
        type: persistent
        size: 1Gi
        mount-path: /data
    aliases:
      POSTGRES_SERVER: $DATABASE_SERVER_HOST
    user-env-vars: |
      -----BEGIN AGE ENCRYPTED FILE-----
      ...
      -----END AGE ENCRYPTED FILE-----
    sso-rijk: true
    publish-on-web: true
```

## Editable Definitions

### Per-component fields

```python
class ProjectEditables:

    # === Components (sequence item fields) ===

    COMPONENT_NAME = ProjectEditable(
        yaml_path="components[*]/name",
        widget="text",
        label="component.name",
        description="component.name.description",
        placeholder="component-1",
        required=True,
        validator=SlugValidator(),
    )

    COMPONENT_TYPE = ProjectEditable(
        yaml_path="components[*]/type",
        widget="select",
        label="component.type",
        description="component.type.description",
        options_provider="ComponentTypeOptionsProvider",
        required=True,
    )

    COMPONENT_PORTS_INBOUND = ProjectEditable(
        yaml_path="components[*]/ports/inbound",
        widget="text",
        label="component.ports.inbound",
        description="component.ports.inbound.description",
        placeholder="8000, 8080",
        converter=IntegerListConverter(),
        required=True,
    )

    COMPONENT_PORTS_OUTBOUND = ProjectEditable(
        yaml_path="components[*]/ports/outbound",
        widget="text",
        label="component.ports.outbound",
        description="component.ports.outbound.description",
        placeholder="80, 443",
        converter=IntegerListConverter(),
    )

    COMPONENT_PATH = ProjectEditable(
        yaml_path="components[*]/path",
        widget="text",
        label="component.path",
        description="component.path.description",
        placeholder="/",
        # Only relevant for types that serve HTTP
        depends_on="components[*]/type",
        show_when={"type": ["single", "frontend"]},
    )

    COMPONENT_RESOURCES_CPU = ProjectEditable(
        yaml_path="components[*]/resources/cpu",
        widget="select",
        label="component.cpu",
        options_provider="CpuLimitOptionsProvider",
    )

    COMPONENT_RESOURCES_MEMORY = ProjectEditable(
        yaml_path="components[*]/resources/memory",
        widget="select",
        label="component.memory",
        options_provider="MemoryLimitOptionsProvider",
    )

    COMPONENT_USES_SERVICES = ProjectEditable(
        yaml_path="components[*]/uses-services",
        widget="checkbox-group",
        label="component.uses_services",
        description="component.uses_services.description",
        options_provider="FilteredServiceOptionsProvider",
        # Options are filtered to project-level services (cross-part dependency)
    )

    COMPONENT_USES_COMPONENTS = ProjectEditable(
        yaml_path="components[*]/uses-components",
        widget="checkbox-group",
        label="component.uses_components",
        # Options populated from other components in this project (self-reference)
    )

    COMPONENT_PUBLISH_ON_WEB = ProjectEditable(
        yaml_path="components[*]/publish-on-web",
        widget="checkbox",
        label="component.publish_on_web",
        description="component.publish_on_web.description",
        # Only shown if publish-on-web is in project services
        depends_on="services",
        show_when={"contains": "publish-on-web"},
    )

    COMPONENT_SSO_RIJK = ProjectEditable(
        yaml_path="components[*]/sso-rijk",
        widget="checkbox",
        label="component.sso_rijk",
        description="component.sso_rijk.description",
        # Only shown if keycloak is in project services
        depends_on="services",
        show_when={"contains": "keycloak"},
    )
```

### Storage sub-sequence

```python
    COMPONENT_STORAGE_NAME = ProjectEditable(
        yaml_path="components[*]/storage[*]/name",
        widget="text",
        label="component.storage.name",
        placeholder="data",
        required=True,
    )

    COMPONENT_STORAGE_TYPE = ProjectEditable(
        yaml_path="components[*]/storage[*]/type",
        widget="select",
        label="component.storage.type",
        # Options: persistent, ephemeral
    )

    COMPONENT_STORAGE_SIZE = ProjectEditable(
        yaml_path="components[*]/storage[*]/size",
        widget="select",
        label="component.storage.size",
        options_provider="StorageSizeOptionsProvider",
    )

    COMPONENT_STORAGE_MOUNT_PATH = ProjectEditable(
        yaml_path="components[*]/storage[*]/mount-path",
        widget="text",
        label="component.storage.mount_path",
        placeholder="/data",
        required=True,
    )

    COMPONENT_STORAGE_SEQUENCE = ProjectEditable(
        yaml_path="components[*]/storage",
        widget="sequence",
        label="component.storage",
        min_items=0,
        children=[
            ProjectEditables.COMPONENT_STORAGE_NAME,
            ProjectEditables.COMPONENT_STORAGE_TYPE,
            ProjectEditables.COMPONENT_STORAGE_SIZE,
            ProjectEditables.COMPONENT_STORAGE_MOUNT_PATH,
        ],
    )
```

### Advanced fields (collapsible)

```python
    COMPONENT_ALIASES = ProjectEditable(
        yaml_path="components[*]/aliases",
        widget="textarea",
        label="component.aliases",
        description="component.aliases.description",
        placeholder="POSTGRES_SERVER=$DATABASE_SERVER_HOST",
        converter=KeyValueConverter(),
    )

    COMPONENT_USER_ENV_VARS = ProjectEditable(
        yaml_path="components[*]/user-env-vars",
        widget="textarea",
        label="component.env_vars",
        description="component.env_vars.description",
        placeholder="KEY=value",
        converter=EncryptedDisplayConverter(),
        # On edit: if value starts with AGE header, render as display-card instead
        readonly_on_edit=True,  # Encrypted on existing projects
    )
```

### Top-level sequence

```python
    COMPONENTS_SEQUENCE = ProjectEditable(
        yaml_path="components",
        widget="sequence",
        label="project.components",
        description="project.components.description",
        min_items=1,
        children=[
            ProjectEditables.COMPONENT_NAME,
            ProjectEditables.COMPONENT_TYPE,
            ProjectEditables.COMPONENT_PORTS_INBOUND,
            ProjectEditables.COMPONENT_PORTS_OUTBOUND,
            ProjectEditables.COMPONENT_PATH,
            ProjectEditables.COMPONENT_RESOURCES_CPU,
            ProjectEditables.COMPONENT_RESOURCES_MEMORY,
            ProjectEditables.COMPONENT_PUBLISH_ON_WEB,
            ProjectEditables.COMPONENT_SSO_RIJK,
            ProjectEditables.COMPONENT_USES_SERVICES,
            ProjectEditables.COMPONENT_STORAGE_SEQUENCE,
            ProjectEditables.COMPONENT_ALIASES,
            ProjectEditables.COMPONENT_USER_ENV_VARS,
        ],
    )
```

## Dependencies in this Part

### Cross-part: `uses-services` options filtering

```
COMPONENT_USES_SERVICES depends on → project-level "services"
Type: Cross-part option filtering (Level 3)

In edit mode:
  project_services = get_value(yaml, "services") → extract service names
  FilteredServiceOptionsProvider.get_options(context={"project_services": names})
  → Only shows services that are enabled at the project level

In create wizard:
  project_services = session["services"]["selected"]  (from step 2)
  → Same filtering
```

### Cross-part: `publish-on-web` / `sso-rijk` visibility

```
COMPONENT_PUBLISH_ON_WEB depends on → "services" contains "publish-on-web"
COMPONENT_SSO_RIJK depends on → "services" contains "keycloak"
Type: Static conditional visibility (Level 1)

At render time, should_render_editable() checks:
  services_list = get_value(yaml, "services")
  service_names = extract_names(services_list)  # handles mixed str/dict
  "publish-on-web" in service_names → show/hide the checkbox
```

### Self-reference: `uses-components`

```
COMPONENT_USES_COMPONENTS depends on → other components[*]/name
Type: Self-reference option filtering (Level 3)

Options populated at render time from other components in the same project,
excluding the current component (to avoid self-reference).
```

### Intra-component: `path` visibility

```
COMPONENT_PATH depends on → components[*]/type
Type: Static conditional visibility (Level 1)

Only shown when type is "single" or "frontend" (types that serve HTTP).
Backend components don't have a URL path.
```

## Layout and Rendering

```python
layout=Fieldset(
    legend="project.components.title",
    children=[
        Sequence(
            field_name="components",
            child_layout=Fieldset(
                legend="component.details",
                children=[
                    # Row 1: Identity
                    Row(children=[
                        Column("name", width=4),          # → render_text()
                        Column("type", width=4),          # → render_select()
                        Column("path", width=4),          # → render_text() (conditional)
                    ]),
                    # Row 2: Ports
                    Row(children=[
                        Column("ports/inbound", width=6),  # → render_text() (IntegerListConverter)
                        Column("ports/outbound", width=6), # → render_text() (IntegerListConverter)
                    ]),
                    # Row 3: Resources
                    Row(children=[
                        Column("resources/cpu", width=6),    # → render_select()
                        Column("resources/memory", width=6), # → render_select()
                    ]),
                    # Row 4: Flags (conditionally shown)
                    Row(children=[
                        Column("publish-on-web", width=6),  # → render_checkbox() (conditional)
                        Column("sso-rijk", width=6),         # → render_checkbox() (conditional)
                    ]),
                    # Services
                    "uses-services",                        # → render_checkbox_group()
                    # Storage (collapsible)
                    Fieldset(
                        legend="component.storage.title",
                        collapsible=True,
                        collapsed=True,
                        children=[
                            Sequence(
                                field_name="storage",
                                child_layout=Row(children=[
                                    Column("name", width=3),
                                    Column("type", width=2),
                                    Column("size", width=3),
                                    Column("mount-path", width=4),
                                ]),
                                min_items=0,
                                add_label="Volume toevoegen",
                            ),
                        ],
                    ),
                    # Advanced (collapsible)
                    Fieldset(
                        legend="component.advanced",
                        collapsible=True,
                        collapsed=True,
                        children=[
                            "aliases",        # → render_textarea() (KeyValueConverter)
                            "user-env-vars",  # → render_textarea() or render_display_card()
                        ],
                    ),
                ],
            ),
            min_items=1,
            add_label="Component toevoegen",
            remove_label="Component verwijderen",
        ),
    ],
)
```

## Part Definition

```python
class ProjectParts:

    COMPONENTS = EditablePart(
        part_id="components",
        title="Componenten",
        icon="blokken",
        description="Definieer de applicatie-componenten",
        editables=[ProjectEditables.COMPONENTS_SEQUENCE],
        layout=...,  # As above
        in_create_wizard=True,
        wizard_step=4,
        enforcer=UniqueNamesEnforcer(path="components[*]/name"),
        summary_fn=components_summary,
    )
```

## Display Summary

```python
def components_summary(data: dict) -> str:
    components = get_value(data, "components") or []
    if not components:
        return "Geen componenten"
    names = [c.get("name", "naamloos") for c in components if isinstance(c, dict)]
    return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
```

## Validation Rules

1. At least one component is required
2. Component names must be unique within the project (`UniqueNamesEnforcer`)
3. Component names must be slug-format (`SlugValidator`)
4. At least one inbound port is required
5. `uses-services` must only reference project-level services (validated by `ServiceDependencyEnforcer`)
6. Storage mount paths must be unique within a component

## Acceptance Criteria

- [ ] Components sequence renders with all nested fields in correct layout
- [ ] Add/remove components works (min 1)
- [ ] Port fields render as text inputs, accept comma-separated integers via `IntegerListConverter`
- [ ] Resource selects show CPU and memory options from providers
- [ ] `uses-services` checkbox-group only shows project-level services (cross-part filtering)
- [ ] `publish-on-web` checkbox only shown if publish-on-web is a project service
- [ ] `sso-rijk` checkbox only shown if keycloak is a project service
- [ ] `path` field only shown for single/frontend types
- [ ] Storage sub-sequence can be added/removed within each component
- [ ] Aliases textarea renders KEY=VALUE format via `KeyValueConverter`
- [ ] Encrypted user-env-vars shown as read-only on edit
- [ ] Validation prevents duplicate component names
- [ ] Nested path resolution works: `components[0]/ports/inbound` resolves correctly
