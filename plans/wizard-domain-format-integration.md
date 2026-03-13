# Plan: Add domain-format to wizards, hide complex fields, add per-domain dot support

## Context

The `domain-format` field exists in editable/visualizer definitions but is **not shown in the create wizard**. The create wizard currently shows `domain-mode` (component-specific, deployment-name, custom, nice-url) plus complex fields like subdomain, base-domain, and root-component.

The goal is to make `domain-format` the primary user-facing control and hide the complex fields when they're not needed. Additionally, not all base domains support dot-separated subdomains, and this needs to be explicitly tracked per-domain and surfaced in the wizard.

### Backend constraint

`domain-mode` is deeply used in the backend (project_manager, keycloak_manager) — it drives `HostnameFormat.from_domain_mode()`, subdomain registration, root component logic, and path validation. We **cannot simply remove** domain-mode. Instead, we'll:
- Make `domain-format` the primary UI control
- **Auto-derive** `domain-mode` from the selected `domain-format` via a generator editable (computed at submit time)
- Keep `domain-mode` in the YAML for backward compatibility with the backend

### Domain format templates (reference)

```
DOMAIN_FORMAT_TEMPLATES = {
    "component-deployment-project":    component-deployment-project.domain    (has {component})
    "component-deployment-subdomain":  component-deployment-subdomain.domain  (has {component})
    "deployment-project":              deployment-project.domain              (NO {component})
    "deployment-subdomain":            deployment-subdomain.domain            (NO {component})
}
```

Each template has a dash variant and a dot variant. The system auto-selects dot vs dash based on the base-domain's dot support.

## Changes

### 1. Add `domain-format` to the create wizard's `DOMAIN_SECTION`

**File:** `opi/forms/visualizers/wizard_sections.py`

Replace `domain-mode` with `domain-format` as the primary control. Remove `domain-mode` from the layout (it will be auto-generated). Keep subdomain, base-domain, and root-component with updated `depends_on` conditions.

```python
DOMAIN_SECTION = FormSection(
    section_id="domains",
    title="Webadres",
    icon="wereld",
    description="Configureer hoe uw applicatie bereikbaar wordt",
    editables=[DOMAIN_FORMAT, DOMAIN_SUBDOMAIN, DOMAIN_BASE_DOMAIN, DOMAIN_ROOT_COMPONENT],
    layout=[
        TemplatePartial(template="wizard/partials/domain_info.html.j2"),
        "deployments[0]/domain-format",
        "deployments[0]/subdomain",
        "deployments[0]/base-domain",
        "deployments[0]/root-component",
    ],
)
```

Import `DOMAIN_FORMAT` from `opi.forms.visualizers.fields.domains`.

### 2. Auto-derive `domain-mode` from `domain-format`

**File:** `opi/forms/editables/fields/domains.py`

Add a generator editable that computes `domain-mode` from `domain-format`:

```python
class DomainModeGenerator:
    """Derive domain-mode from the selected domain-format template."""

    def generate(self, yaml_data: dict[str, Any]) -> str:
        deployments = yaml_data.get("deployments", [])
        if not deployments:
            return "component-specific"
        domain_format = deployments[0].get("domain-format", "")
        return _infer_domain_mode(domain_format)
```

Reverse-map logic (using `DOMAIN_MODE_DEFAULT_FORMAT`):
- Formats with `{subdomain}` + base-domain → `"nice-url"` or `"custom"` depending on base-domain dot support
- `component-deployment-project` → `"component-specific"`
- `deployment-project` → `"deployment-name"`

Register in `opi/forms/editables/fields/config_generated.py` → `GENERATED_EDITABLES_PURE`.

### 3. Update `DOMAIN_FORMAT_EDITABLE` — remove dependency on domain-mode

**File:** `opi/forms/editables/fields/domains.py`

Currently `DOMAIN_FORMAT_EDITABLE` has `depends_on="deployments[0]/domain-mode"`. Since domain-mode is no longer shown, remove this dependency:

```python
DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path="deployments[0]/domain-format",
    values_provider="DomainFormatOptionsProvider",
    required=True,
    default="component-deployment-project",
    validator=DomainFormatValidator(),
)
```

### 4. Update `DomainFormatOptionsProvider` — show all options

**File:** `opi/forms/visualizers/providers.py`

Remove the `domain_mode` filtering. Show all 4 template IDs. The dot vs dash variant is auto-selected at deploy time — user doesn't choose it explicitly.

```python
class DomainFormatOptionsProvider:
    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "component-deployment-project",
             "label": "component-deployment-project.domein",
             "description": "Elk component krijgt een eigen URL (standaard)"},
            {"value": "component-deployment-subdomain",
             "label": "component-deployment-subdomain.domein",
             "description": "Eigen URL per component met een subdomein"},
            {"value": "deployment-project",
             "label": "deployment-project.domein",
             "description": "Alle componenten op dezelfde URL, verschillende paden"},
            {"value": "deployment-subdomain",
             "label": "deployment-subdomain.domein",
             "description": "Gedeelde URL met subdomein, verschillende paden"},
        ]
```

### 5. Update `depends_on`/`show_when` for complex domain fields

**File:** `opi/forms/editables/fields/domains.py`

Update the conditional fields to depend on `domain-format` instead of `domain-mode`. Use `show_when` with value matching (domain-format is a string, not a list):

```python
# Show subdomain only for formats that use {subdomain}
DOMAIN_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/subdomain",
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["component-deployment-subdomain", "deployment-subdomain"]},
    validator=SubdomainValidator(),
)

# Show base-domain only for formats that use {subdomain} (these need a custom domain)
DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[0]/base-domain",
    values_provider="ClusterBaseDomainOptionsProvider",
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["component-deployment-subdomain", "deployment-subdomain"]},
    validator=BaseDomainValidator(),
)

# Show root-component for formats WITHOUT {component} — user must pick which
# component gets the "bare" hostname (others are routed by path)
DOMAIN_ROOT_COMPONENT_EDITABLE = Editable(
    yaml_path="deployments[0]/root-component",
    values_provider="ComponentReferenceOptionsProvider",
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["deployment-subdomain", "deployment-project"]},
)
```

### 6. Cross-step dependency: show path/rewrite-path on components when domain-format requires it

**Files:**
- `opi/forms/editables/fields/components.py`
- `opi/forms/visualizers/fields/components.py`

This is the tricky cross-step dependency. When the domain-format does NOT include `{component}` (i.e., `deployment-project` or `deployment-subdomain`), all components share the same hostname and need distinct **paths** (e.g., `/`, `/api`) plus optional **rewrite-path** to be routed correctly.

When the format DOES include `{component}`, each component gets its own hostname and paths are unnecessary.

**Implementation:** `should_render_editable` uses `smart_get_value(yaml_data, depends_on)` on the merged YAML data (which includes data from all wizard steps). So a component-level field can depend on a deployment-level field:

```python
# components.py editables
COMPONENT_PATH_EDITABLE = Editable(
    yaml_path="components[*]/path",
    default="/",
    validator=PathValidator(),
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["deployment-project", "deployment-subdomain"]},
)

COMPONENT_REWRITE_PATH_EDITABLE = Editable(
    yaml_path="components[*]/rewrite-path",
    validator=PathValidator(),
    depends_on="deployments[0]/domain-format",
    show_when={"value": ["deployment-project", "deployment-subdomain"]},
)
```

This hides path/rewrite-path when the domain-format includes `{component}` (each component has its own unique hostname, no path routing needed). It shows them when components share a hostname.

**Note:** The processor fix from the previous bug (should_render_editable check in `_process_sequence_json`) ensures these hidden fields won't be accidentally written to the YAML.

### 7. Add per-domain dot support to cluster config

**File:** `opi/core/cluster_config.py`

Change `nice_url.supported_domains` from a flat list to a list of dicts with dot support metadata:

```python
"nice_url": {
    "supported_domains": [
        {"domain": "kind", "supports_dots": True},
        {"domain": "local", "supports_dots": True},
    ],
},
```

For sandboxed-local:
```python
"nice_url": {
    "supported_domains": [
        {"domain": "sandbox.rijksapp.dev", "supports_dots": True},
        {"domain": "rijksapp.nl", "supports_dots": True},
        {"domain": "rijksapp.dev", "supports_dots": True},
    ],
},
```

For production:
```python
"nice_url": {
    "supported_domains": [
        {"domain": "rijks.app", "supports_dots": True},
        {"domain": "rijksapps.nl", "supports_dots": True},
        {"domain": "rijksapp.nl", "supports_dots": False},
        {"domain": "rijksapp.dev", "supports_dots": True},
    ],
},
```

Update helper functions:
- `get_nice_url_supported_domains(cluster)` → return domain strings only (backward compat)
- Add `get_domain_supports_dots(cluster, domain) -> bool` helper
- Update `is_nice_url_domain_supported()` to work with new format

### 8. Update `ClusterBaseDomainOptionsProvider` to include dot-support info

**File:** `opi/forms/visualizers/providers.py`

Include `supports_dots` in option attributes so the UI can display which domains support dot subdomains:

```python
options.append({
    "value": domain_name,
    "label": f"{domain_name} {'(punt-subdomeinen)' if supports_dots else '(geen punt-subdomeinen)'}",
    "description": "Ondersteunt punt-gescheiden subdomeinen" if supports_dots else "Alleen streepje-gescheiden",
})
```

### 9. Add cross-validation enforcer for dot-format + non-dot-domain

**File:** `opi/forms/editables/enforcers.py`

Add a `DomainDotSupportEnforcer` that checks: if the selected base-domain does not support dots, the system will use the dash variant. This is informational — not an error per se (the system auto-falls back to dashes). But the user should know the resulting URL pattern differs from what the dot labels suggest.

This enforcer should be a **section-level enforcer** on the domain section.

### 10. Update domain info partial

**File:** `opi/templates/wizard/partials/domain_info.html.j2`

Simplify the info text to explain domain-format options:
- Formats with `{component}` → each component gets its own URL
- Formats without `{component}` → components share a URL, routed by path
- Formats with `{subdomain}` → you choose a subdomein and basisdomein
- Formats with `{project}` → project name is used in the URL
- Add note about dot support: not all base domains support punt-gescheiden subdomeinen

### 11. Update deployment edit form (DEPLOYMENTS_SEQUENCE)

**File:** `opi/forms/editables/fields/deployments.py`

Apply same changes as the wizard:
- Update `depends_on`/`show_when` for subdomain, base-domain, domain-mode to reference domain-format
- Make domain-mode a generated/hidden field (or remove from the edit sequence)

### 12. Migration for existing projects

When editing an existing project that has `domain-mode` but no `domain-format`:
- The edit wizard should detect this and pre-fill `domain-format` from `DOMAIN_MODE_DEFAULT_FORMAT`:
  - `"nice-url"` → `"component-deployment-subdomain"`
  - `"component-specific"` → `"component-deployment-project"`
  - `"deployment-name"` → `"deployment-project"`
  - `"custom"` → `"deployment-subdomain"`

This can be done as a **converter** on `DOMAIN_FORMAT_EDITABLE` that reads the current value and falls back to deriving it from `domain-mode` if empty. Or as part of the edit flow's data loading.

### 13. Tests

- Test `DomainModeGenerator` correctly derives mode from format
- Test updated `DomainFormatOptionsProvider` returns all 4 options
- Test `DomainDotSupportEnforcer` validates dot-format + non-dot-domain
- Test updated `show_when` conditions on subdomain/base-domain/root-component
- Test cross-step dependency: path/rewrite-path visibility based on domain-format
- Test backward compatibility: existing projects without domain-format still work
- Test migration: domain-format auto-populated from domain-mode for existing projects

## Summary of dependency chain

```
domain-format (domain section)
├── subdomain          → shown when format uses {subdomain}
├── base-domain        → shown when format uses {subdomain}
├── root-component     → shown when format has NO {component}
├── domain-mode        → auto-generated at submit time (backend compat)
└── component path/rewrite-path (components section, cross-step)
    → shown when format has NO {component} (shared hostname → need paths)
    → hidden when format has {component} (unique hostnames → no paths needed)
```

## Files to Modify

1. `opi/forms/visualizers/wizard_sections.py` — Replace domain-mode with domain-format in section
2. `opi/forms/editables/fields/domains.py` — Update editables, add generator
3. `opi/forms/editables/fields/components.py` — Add depends_on for path/rewrite-path
4. `opi/forms/editables/fields/deployments.py` — Update depends_on for deployment edit
5. `opi/forms/editables/fields/config_generated.py` — Register domain-mode generator
6. `opi/forms/visualizers/fields/domains.py` — Update DOMAIN_FORMAT attributes
7. `opi/forms/visualizers/providers.py` — Update DomainFormatOptionsProvider, ClusterBaseDomainOptionsProvider
8. `opi/core/cluster_config.py` — Add per-domain dot support metadata
9. `opi/forms/editables/enforcers.py` — Add dot-support cross-validation
10. `opi/templates/wizard/partials/domain_info.html.j2` — Update info text
11. Tests

## Verification

```bash
uv run ruff check opi/forms/ opi/core/cluster_config.py opi/utils/naming.py
uv run ruff format opi/forms/ opi/core/cluster_config.py opi/utils/naming.py
uv run pytest tests/forms/ tests/test_domain_format.py tests/test_editables_providers.py tests/test_editable_processor.py -q
```
