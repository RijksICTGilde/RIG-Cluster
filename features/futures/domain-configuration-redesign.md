# Domain Configuration Redesign

**Status**: Planned
**Priority**: Medium
**Created**: 2026-02-04

## Overview

This document outlines the current domain configuration options and proposes improvements to the UI/flow for domain configuration in the self-service portal. The goal is to provide a clearer, more flexible domain configuration experience.

## Current Domain Modes

### 1. Per-Component URLs (Default / Component-Specific)

Each component gets its own unique URL using dash-separated naming:

```
component1-deployment-projectname.domain.ext
component2-deployment-projectname.domain.ext
```

**Use case**: Components that are unrelated or standalone, for example:
- `frontend-productie-amt.rig.rijksapps.nl`
- `documentatie-productie-amt.rig.rijksapps.nl`

**Configuration**:
```yaml
deployments:
  - name: productie
    cluster: odcn-production
    components:
      - reference: frontend
      - reference: documentatie
    # No subdomain, no base-domain, no domain-mode
```

### 2. Per-Deployment URL (Path-Based)

All components share a single deployment URL, differentiated by paths:

```
deployment-projectname.domain.ext/           -> component1
deployment-projectname.domain.ext/docs       -> component2
```

**Use case**: Components that belong together under one domain, for example:
- `productie-amt.rig.rijksapps.nl/` -> frontend
- `productie-amt.rig.rijksapps.nl/documentatie` -> docs component

**Note**: When using this mode with multiple components, paths MUST be configured.

**Configuration**:
```yaml
deployments:
  - name: productie
    subdomain: productie         # subdomain = deployment name
    components:
      - reference: frontend
        paths:
          - /
      - reference: documentatie
        paths:
          - /documentatie
```

### 3. Custom Subdomain (External Domain)

User-defined subdomain on a supported base domain:

```
customname.domain.ext
```

**Use case**: Projects wanting a clean, branded URL, for example:
- `wies.rijksapp.nl`

**Configuration**:
```yaml
deployments:
  - name: staging2
    subdomain: wies
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
```

### 4. Nice URLs (Dotted Format)

When `domain-mode: nice-url` is enabled, URLs use dot-separation instead of dashes:

```
component.subdomain.base-domain
```

**Examples**:
- `frontend.myapp.rijksapp.nl`
- `backend.myapp.rijksapp.nl`
- `myapp.rijksapp.nl` (root URL, requires `root: true` on component)

**Configuration**:
```yaml
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
        root: true              # Responds to myapp.rijksapp.nl
      - reference: backend      # Responds to backend.myapp.rijksapp.nl
```

## Supported Base Domains

| Cluster | Supported Domains |
|---------|-------------------|
| local | `kind`, `local` |
| odcn-production | `rijks.app`, `rijksapps.nl`, `rijksapp.nl` |

---

## Design Decisions (Resolved)

### 1. Subdomain Extensibility

**Decision**: Allow custom subdomain overrides in nice-url mode.

The project name is often a generated unique identifier (like `amt-2m9`), which isn't user-friendly for URLs.

**Solution**: The `subdomain` field already supports custom values. No change needed to the data model - just ensure the wizard UI makes this clear:

```yaml
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain: bzk              # Custom subdomain instead of project name
    base-domain: rijksapp.nl
```

Result: `frontend.bzk.rijksapp.nl` instead of `frontend.amt-2m9.rijksapp.nl`

### 2. Root Domain + Sub-Subdomains

**Decision**: Support hierarchical subdomains via dot-notation in the `subdomain` field for non-nice-url mode only. Nice-url mode keeps strict single-level subdomains.

**Rationale**: Nice-url mode already uses dots for component separation (`component.subdomain.domain`). Allowing dots in the subdomain would create ambiguity (`is.this.a.component.or.subdomain.domain`). For hierarchical use cases, use non-nice-url mode with paths or separate deployments.

```yaml
# This works (non-nice-url mode):
deployments:
  - name: bzk-amt
    subdomain: amt.bzk           # Hierarchical subdomain
    base-domain: rijksapp.nl
    # Result: amt.bzk.rijksapp.nl

# This is NOT allowed (nice-url mode):
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain: amt.bzk           # ERROR: dots not allowed in nice-url subdomain
```

### 3. Cross-Project Subdomain Sharing

**Decision**: Allow shared subdomain "groups" via a `subdomain-group` field. Projects register under a parent subdomain managed by a designated owner project.

```yaml
# Owner project (bzk-portal) registers the group:
name: bzk-portal
subdomain-groups:
  - subdomain: bzk
    base-domain: rijksapp.nl

# Member projects reference the group:
name: amt
deployments:
  - name: productie
    domain-mode: nice-url
    subdomain-group: bzk         # Uses bzk.rijksapp.nl as parent
    subdomain: amt               # Result: amt.bzk.rijksapp.nl
    base-domain: rijksapp.nl
```

**Subdomain registry validation**: The subdomain registry (already exists for nice-url) is extended to track group ownership:

```sql
-- Existing subdomain_registry table gets a new column:
ALTER TABLE subdomain_registry ADD COLUMN group_owner VARCHAR(63);
-- group_owner = project_name that owns the group, or NULL for standalone
```

### 4. Wildcard Certificates

**Decision**: Use wildcard certificates for nice-url mode with many components.

```yaml
# When a deployment has 3+ components in nice-url mode,
# automatically request *.subdomain.base-domain
# instead of individual certs per component
```

This requires DNS-01 challenge (not HTTP-01). The cert-manager configuration already supports this via the `issuer: letsencrypt` field which uses DNS-01 on production clusters.

---

## Implementation

### Phase 1: Wizard UI Improvements

**File**: `opi/forms/visualizers/wizard_sections.py` (modify)

Add a clearer domain configuration section to the deployment wizard:

```python
DOMAIN_SECTION = FormSection(
    section_id="domain",
    title="Domein configuratie",
    icon="globe",
    description="Kies hoe uw applicatie bereikbaar wordt",
    editables=[
        # Step 1: Base domain selection
        Editable(
            name="base-domain",
            yaml_path="deployments[*].base-domain",
            widget="select",
            options=lambda cluster: get_supported_domains(cluster),
            label="Basisdomein",
            help_text="Het hoofddomein waaronder uw applicatie bereikbaar wordt",
        ),
        # Step 2: URL structure choice
        Editable(
            name="domain-mode",
            yaml_path="deployments[*].domain-mode",
            widget="radio",
            options=[
                {"value": "", "label": "Per component (aparte URLs)"},
                {"value": "path", "label": "Per deployment (pad-gebaseerd)"},
                {"value": "nice-url", "label": "Aangepast subdomein (mooie URLs)"},
            ],
            label="URL-structuur",
        ),
        # Step 3: Subdomain (conditional)
        Editable(
            name="subdomain",
            yaml_path="deployments[*].subdomain",
            widget="text",
            label="Subdomein",
            help_text="Kies een uniek subdomein",
            visible_when={"domain-mode": ["nice-url", "path"]},
            validators=["dns_label", "subdomain_available"],
        ),
        # Step 4: Root component (conditional, nice-url only)
        Editable(
            name="root-component",
            yaml_path="deployments[*].components[*].root",
            widget="select",
            label="Hoofd-component",
            help_text="Dit component reageert op het root-domein",
            visible_when={"domain-mode": "nice-url"},
            options_from="components",
        ),
    ],
    layout=[
        "base-domain",
        "domain-mode",
        "subdomain",
        "root-component",
        # URL preview (rendered dynamically)
        "url-preview",
    ],
)
```

### Phase 2: URL Preview Component

**File**: `opi/web/templates/components/url-preview.html` (new)

HTMX-powered live preview that updates as the user changes domain settings:

```html
<div id="url-preview" class="rvo-card">
  <h4>Gegenereerde URLs</h4>
  <div hx-get="/api/forms/preview-urls"
       hx-trigger="change from:[name='domain-mode'], change from:[name='subdomain']"
       hx-include="[name='domain-mode'],[name='subdomain'],[name='base-domain']">
    <ul>
    {% for component in components %}
      <li>
        <code>{{ component.url }}</code>
        <span class="rvo-text--subtle">→ {{ component.name }}</span>
      </li>
    {% endfor %}
    </ul>
  </div>
</div>
```

**File**: `opi/api/form_router.py` (modify)

Add preview endpoint:

```python
@form_router.get("/preview-urls")
async def preview_urls(
    domain_mode: str = Query(""),
    subdomain: str = Query(""),
    base_domain: str = Query(""),
    cluster: str = Query("local"),
) -> HTMLResponse:
    """Generate URL preview for the domain configuration wizard."""
    # Use existing URL generation logic from ingress template
    urls = generate_url_preview(domain_mode, subdomain, base_domain, cluster)
    return templates.TemplateResponse("components/url-preview.html", {"urls": urls})
```

### Phase 3: Subdomain Validation Improvements

**File**: `opi/core/subdomain_registry.py` (modify)

Extend validation to handle the new patterns:

```python
async def validate_subdomain(
    self, subdomain: str, base_domain: str, project_name: str, domain_mode: str
) -> tuple[bool, str]:
    """Validate subdomain availability and format."""
    # Format validation
    if domain_mode == "nice-url":
        # Strict: a-z, 0-9, hyphens only, no dots
        if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', subdomain):
            return False, "Nice-URL subdomein mag alleen letters, cijfers en streepjes bevatten"
        if len(subdomain) > 63:
            return False, "Subdomein mag maximaal 63 tekens zijn"
    else:
        # Flexible: dots allowed for hierarchical subdomains
        if not re.match(r'^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$', subdomain):
            return False, "Subdomein bevat ongeldige tekens"

    # Availability check
    existing = await self.get_subdomain_owner(subdomain, base_domain)
    if existing and existing != project_name:
        return False, f"Subdomein '{subdomain}.{base_domain}' is al in gebruik door project '{existing}'"

    # Group check
    if "." in subdomain and domain_mode != "nice-url":
        parent = subdomain.split(".", 1)[1]
        group_owner = await self.get_group_owner(parent, base_domain)
        if group_owner and group_owner != project_name:
            # Check if project is allowed in this group
            allowed = await self.is_group_member(parent, base_domain, project_name)
            if not allowed:
                return False, f"Subdomein groep '{parent}.{base_domain}' wordt beheerd door '{group_owner}'"

    return True, ""
```

### Phase 4: Subdomain Group Registry

**File**: `opi/core/subdomain_registry.py` (modify)

Add group management methods:

```python
async def register_subdomain_group(
    self, subdomain: str, base_domain: str, owner_project: str
) -> bool:
    """Register a subdomain group (e.g., 'bzk' on 'rijksapp.nl')."""
    query = """
        INSERT INTO subdomain_registry (subdomain, base_domain, project_name, is_group, group_owner)
        VALUES ($1, $2, $3, TRUE, $3)
        ON CONFLICT (subdomain, base_domain) DO NOTHING
        RETURNING id
    """
    result = await self.pool.fetchval(query, subdomain, base_domain, owner_project)
    return result is not None

async def add_group_member(
    self, group_subdomain: str, base_domain: str, member_project: str
) -> bool:
    """Allow a project to use subdomains under a group."""
    query = """
        INSERT INTO subdomain_group_members (group_subdomain, base_domain, project_name)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
    """
    await self.pool.execute(query, group_subdomain, base_domain, member_project)
    return True
```

Schema addition:

```sql
ALTER TABLE subdomain_registry ADD COLUMN IF NOT EXISTS is_group BOOLEAN DEFAULT FALSE;
ALTER TABLE subdomain_registry ADD COLUMN IF NOT EXISTS group_owner VARCHAR(63);

CREATE TABLE IF NOT EXISTS subdomain_group_members (
    id SERIAL PRIMARY KEY,
    group_subdomain VARCHAR(63) NOT NULL,
    base_domain VARCHAR(255) NOT NULL,
    project_name VARCHAR(63) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (group_subdomain, base_domain, project_name)
);
```

### Phase 5: Wildcard Certificate Logic

**File**: `opi/manager/ingress_manager.py` (modify)

```python
def should_use_wildcard_cert(self, deployment: dict) -> bool:
    """Use wildcard cert when nice-url mode has 3+ components."""
    if deployment.get("domain-mode") != "nice-url":
        return False
    components = deployment.get("components", [])
    return len(components) >= 3

def get_tls_config(self, deployment: dict) -> dict:
    subdomain = deployment.get("subdomain", "")
    base_domain = deployment.get("base-domain", "")

    if self.should_use_wildcard_cert(deployment):
        return {
            "hosts": [f"*.{subdomain}.{base_domain}"],
            "secret_name": f"{subdomain}-{base_domain.replace('.', '-')}-wildcard-tls",
            "issuer": deployment.get("issuer", "letsencrypt"),
        }
    else:
        # Individual certs per component (existing behavior)
        return None  # Use existing per-host cert logic
```

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `opi/web/templates/components/url-preview.html` | HTMX URL preview component |

### Modified Files

| File | Change |
|------|--------|
| `opi/forms/visualizers/wizard_sections.py` | Redesigned DOMAIN_SECTION with radio selection + conditional fields |
| `opi/api/form_router.py` | Add `/preview-urls` endpoint |
| `opi/core/subdomain_registry.py` | Group support + enhanced validation |
| `opi/core/startup.py` | Create `subdomain_group_members` table |
| `opi/manager/ingress_manager.py` | Wildcard certificate logic for nice-url |

---

## Migration Path

- All existing project configurations continue to work unchanged
- New features (groups, wildcard certs) are opt-in via explicit configuration
- The wizard UI defaults to the existing component-specific mode
- No breaking changes to the YAML schema - only additive fields

## Dependencies

- [external-domains-letsencrypt.md](../external-domains-letsencrypt.md) - Let's Encrypt integration for custom domains (implemented)
- [multi-path-ingress.md](../multi-path-ingress.md) - Multiple paths per component (implemented)
- Subdomain registry (existing in `opi/core/subdomain_registry.py`)
- cert-manager with DNS-01 challenge support (existing in production)

## Verification

1. **Wizard flow**: Create a new project, select each domain mode, verify URL preview updates
2. **Nice-URL**: Configure `domain-mode: nice-url` with 3 components, verify wildcard cert is requested
3. **Hierarchical subdomain**: Use `subdomain: amt.bzk` in non-nice-url mode, verify URL resolves
4. **Group registration**: Create group `bzk` from project A, add project B as member, verify B can use `amt.bzk.rijksapp.nl`
5. **Validation**: Try registering a taken subdomain, verify error message
6. **Backwards compatibility**: Deploy existing project configs, verify no changes to URLs
