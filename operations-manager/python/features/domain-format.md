# Domain Format (Configurable Hostname Patterns)

## What it is

The `domain-format` field allows users to choose how hostnames are constructed for their deployment URLs. Instead of being locked into the default hostname pattern for each domain mode, users can select from predefined templates that control which variables appear in the hostname.

## How it works

Each deployment can optionally include a `domain-format` field in its YAML configuration. When set, hostnames are generated from the selected template instead of the default logic.

### Available formats

| Format ID | Dash variant | Dot variant |
|---|---|---|
| `component-deployment-project` | `frontend-poc-myapp.kind` | `frontend.poc.myapp.rijksapp.dev` |
| `component-deployment-subdomain` | `frontend-poc-moza.kind` | `frontend.poc.moza.rijksapp.dev` |
| `deployment-project` | `poc-myapp.kind` | `poc.myapp.rijksapp.dev` |
| `deployment-subdomain` | `poc-moza.kind` | `poc.moza.rijksapp.dev` |

- **Dash variant** is used for clusters without nice-URL support
- **Dot variant** is used when the cluster supports nice URLs (dotted hostnames)

### Domain resolution

The `{domain}` part of the hostname is resolved as:
1. `base-domain` from the deployment YAML if set (e.g., `rijksapp.dev`)
2. Otherwise, the cluster's `ingress_postfix` with leading dot stripped (e.g., `kind`)

## Configuration

Add `domain-format` to a deployment in your project YAML:

```yaml
deployments:
  - name: productie
    cluster: local
    domain-mode: nice-url
    domain-format: deployment-subdomain    # <-- new field
    subdomain: moza
    base-domain: rijksapp.dev
    components:
      - reference: frontend
        image: myapp/frontend:latest
```

## Backward compatibility

- The field is completely optional
- When `domain-format` is absent, the existing hostname generation logic is used unchanged
- No migration is needed for existing projects

## Form integration

The field appears as a SELECT dropdown in both the create wizard and the deployment edit form:
- Label: "URL-formaat"
- Options are filtered by domain mode: nice-url shows dot-separated labels, other modes show dash-separated labels
- The field depends on `domain-mode` and updates when domain mode changes

## Root component behavior

When `domain-format` is set and the template does not include `{component}` (e.g., `deployment-subdomain`), the root component ingress is skipped since all components already share the same hostname. When the template includes `{component}`, root component marking still works as before.
