# Domain Format (Primary Wizard Control)

## What it is

The `domain-format` field is the primary UI control for configuring how hostnames are constructed for deployment URLs. It replaces the old `domain-mode` selector in the create wizard. The backend `domain-mode` value is now auto-derived from `domain-format` via a generator for backward compatibility.

## How it works

Each deployment includes a `domain-format` field that determines:
1. The hostname pattern (which variables appear in the URL)
2. Whether subdomain/base-domain fields are shown
3. Whether path/rewrite-path fields are shown for components
4. Whether a root-component selector is available

### Available formats

| Format ID | Example URL | Per-component URL? | Needs subdomain? |
|---|---|---|---|
| `component-deployment-project` | `frontend-poc-myapp.kind` | Yes | No |
| `component-deployment-subdomain` | `frontend-poc-moza.kind` | Yes | Yes |
| `deployment-project` | `poc-myapp.kind` | No (shared, use paths) | No |
| `deployment-subdomain` | `poc-moza.kind` | No (shared, use paths) | Yes |

### Conditional field visibility

Based on the selected format:
- **Subdomain + Base domain**: Shown when format contains `-subdomain` (i.e., `component-deployment-subdomain` or `deployment-subdomain`)
- **Root component**: Shown when format is shared (i.e., `deployment-project` or `deployment-subdomain`)
- **Component path + rewrite-path**: Shown when format is shared (cross-step dependency from domain section to components section)

### Domain mode auto-derivation

A `DomainModeGenerator` automatically derives `domain-mode` from `domain-format` at submit time:

| domain-format | Derived domain-mode |
|---|---|
| `component-deployment-project` | `component-specific` |
| `component-deployment-subdomain` | `nice-url` |
| `deployment-project` | `deployment-name` |
| `deployment-subdomain` | `custom` |

### Domain resolution

The `{domain}` part of the hostname is resolved as:
1. `base-domain` from the deployment YAML if set (e.g., `rijksapp.dev`)
2. Otherwise, the cluster's `ingress_postfix` with leading dot stripped (e.g., `kind`)

### Per-domain dot support

Each cluster's `supported_domains` list now includes `supports_dots` metadata:
```python
"supported_domains": [
    {"domain": "kind", "supports_dots": True},
    {"domain": "local", "supports_dots": True},
]
```

Use `get_domain_supports_dots(cluster_name, domain)` to check if a domain supports dot-separated hostnames.

## Configuration

Add `domain-format` to a deployment in your project YAML:

```yaml
deployments:
  - name: productie
    cluster: local
    domain-format: deployment-subdomain
    subdomain: moza
    base-domain: rijksapp.dev
    components:
      - reference: frontend
        image: myapp/frontend:latest
```

The `domain-mode` field is auto-generated and does not need to be specified.

## Form integration

The field appears as a SELECT dropdown in the create wizard's "Webadres" section:
- Label: "URL-formaat"
- Default: `component-deployment-project`
- Required: Yes
- Has `data-rerender="true"` to trigger re-rendering of dependent fields

## Root component behavior

When `domain-format` is set and the template does not include `{component}` (e.g., `deployment-subdomain`), the root component ingress is skipped since all components already share the same hostname. When the template includes `{component}`, root component marking still works as before.
