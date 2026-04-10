# Plan: Add `domain-format` field for configurable hostname patterns

## Context

Currently, the hostname pattern is hardcoded per `domain-mode`. For example, `nice-url` always produces `component.subdomain.base_domain`. Users cannot get `deployment.subdomain.base_domain` (e.g., `poc.moza.rijksapp.dev`) without naming the component after the deployment. A new optional `domain-format` field lets users pick from predefined hostname templates, giving flexibility to compose URLs from available variables (`{component}`, `{deployment}`, `{project}`, `{subdomain}`, `{base_domain}`).

## Changes

### 1. Add `DOMAIN_FORMAT_TEMPLATES` dict and defaults to `naming.py`
**File:** `opi/utils/naming.py` (after line 36)

Add a dict mapping template IDs to format strings:
```python
DOMAIN_FORMAT_TEMPLATES = {
    # DOTS-mode (nice-url) templates
    "component-subdomain-base": "{component}.{subdomain}.{base_domain}",
    "deployment-subdomain-base": "{deployment}.{subdomain}.{base_domain}",
    "component-deployment-subdomain-base": "{component}-{deployment}.{subdomain}.{base_domain}",
    "subdomain-base": "{subdomain}.{base_domain}",
    # DASHES-mode templates
    "component-deployment-project-cluster": "{component}-{deployment}-{project}{cluster_postfix}",
    "deployment-project-cluster": "{deployment}-{project}{cluster_postfix}",
    "subdomain-cluster": "{subdomain}{cluster_postfix}",
}
```

And a default mapping for backward compat:
```python
DOMAIN_MODE_DEFAULT_FORMAT = {
    "nice-url": "component-subdomain-base",
    "component-specific": "component-deployment-project-cluster",
    "deployment-name": "deployment-project-cluster",
    "custom": "subdomain-cluster",
}
```

### 2. Add `domain_format` parameter to `get_component_ingress_map()`
**File:** `opi/utils/naming.py` (line 1507)

Add optional `domain_format: str | None = None` parameter. When set and found in `DOMAIN_FORMAT_TEMPLATES`, resolve hostname from the template directly (using `_sanitize_for_lowercase` on interpolated values). When `None`, fall through to existing dispatch logic (no behavioral change).

### 3. Add `domain_format` parameter to `get_deployment_hostnames()`
**File:** `opi/utils/naming.py` (line 1572)

Pass `domain_format` through to `get_component_ingress_map()`. Adjust root hostname logic: only add root hostname when `domain_format` is not set or when the template contains `{component}` (templates without `{component}` already produce the "root" URL).

### 4. Read `domain-format` from deployment config in project_manager
**File:** `opi/manager/project_manager.py` (around line 4049)

Read `domain_format = deployment.get("domain-format")` and pass to `get_component_ingress_map()` and `get_deployment_hostnames()` calls. Also update root-component ingress logic (~line 4472) to skip root ingress when template doesn't contain `{component}`.

### 5. Add `DomainFormatOptionsProvider`
**File:** `opi/forms/visualizers/providers.py`

New provider class that returns DOTS templates for `nice-url` mode, DASHES templates for other modes, all templates when no mode specified. Register in `PROVIDER_REGISTRY`.

Options (Dutch labels):
- DOTS: `component.subdomain.domein`, `deployment.subdomain.domein`, `component-deployment.subdomain.domein`, `subdomain.domein`
- DASHES: `component-deployment-project.cluster`, `deployment-project.cluster`, `subdomain.cluster`

### 6. Add `DomainFormatValidator`
**File:** `opi/forms/editables/validators.py`

Validate that the value is a known key in `DOMAIN_FORMAT_TEMPLATES`.

### 7. Add editable definitions
**File:** `opi/forms/editables/fields/deployments.py`
- Add `DEPLOYMENT_DOMAIN_FORMAT_EDITABLE` with `values_provider="DomainFormatOptionsProvider"`, `depends_on="deployments[*]/domain-mode"`, and `DomainFormatValidator`
- Add to `DEPLOYMENTS_SEQUENCE_EDITABLE` children (after domain-mode)

**File:** `opi/forms/editables/fields/domains.py`
- Add `DOMAIN_FORMAT_EDITABLE` for wizard (path `deployments[0]/domain-format`)

### 8. Add visualizer definitions
**File:** `opi/forms/visualizers/fields/deployments.py`
- Add `DEPLOYMENT_DOMAIN_FORMAT` visualizer (SELECT widget, label "URL-formaat")
- Add to `DEPLOYMENTS_SEQUENCE` children (after domain-mode)

**File:** `opi/forms/visualizers/fields/domains.py`
- Add `DOMAIN_FORMAT` visualizer for wizard

### 9. Tests
**File:** `tests/test_domain_format.py` (new)

- Template resolution: each template ID produces expected hostname
- `get_component_ingress_map` with `domain_format` takes precedence over default dispatch
- Backward compat: `domain_format=None` produces identical results for all 4 modes
- `get_deployment_hostnames` root hostname inclusion/exclusion
- `DomainFormatOptionsProvider` filtering by mode
- `DomainFormatValidator` accepts valid IDs, rejects unknown

## Backward Compatibility
- `domain-format` is optional. Existing project files without it work exactly as before.
- When `domain_format=None`, all functions fall through to existing logic.
- No migration needed.

## Verification
```bash
uv run ruff check opi/utils/naming.py opi/manager/project_manager.py opi/forms/
uv run ruff format opi/utils/naming.py opi/manager/project_manager.py opi/forms/
uv run python -m pytest tests/test_domain_format.py tests/test_nice_url_naming.py tests/forms/ -q
```
