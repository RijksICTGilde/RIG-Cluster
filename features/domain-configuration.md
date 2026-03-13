# Domain Configuration

## Overview

Domain configuration controls how project components are accessible via URLs. Configuration is **per-deployment** — each deployment (e.g., production, staging, feature branches) independently defines its URL strategy.

## Domain Modes

Four domain modes determine how hostnames are constructed:

| Mode | Description | Example URL |
|------|-------------|-------------|
| `component-specific` | Each component gets a unique hostname based on its name | `frontend-myproject.cluster.example.com` |
| `deployment-name` | All components share the deployment name as subdomain | `production.cluster.example.com/frontend` |
| `custom` | User specifies a custom subdomain | `myapp.cluster.example.com` |
| `nice-url` | Dot-separated URLs with a registered domain | `frontend.myapp.rijksapp.nl` |

### Component-Specific Mode (Default)

Components each get a unique hostname using the pattern `{component}-{deployment}-{project}.{cluster_domain}`:

```yaml
deployments:
  - name: main
    cluster: local
    domain-mode: component-specific
    components:
      - reference: frontend
        image: nginx:latest
      - reference: api
        image: myapi:latest
# Results in:
#   frontend-main-myproject.kind
#   api-main-myproject.kind
```

### Deployment-Name Mode

All components share the same hostname, differentiated by path:

```yaml
deployments:
  - name: production
    cluster: local
    domain-mode: deployment-name
    components:
      - reference: frontend
        image: nginx:latest        # production-myproject.kind/
      - reference: api
        image: myapi:latest        # production-myproject.kind/api
```

### Custom Mode

User-specified subdomain replaces the default hostname prefix:

```yaml
deployments:
  - name: production
    cluster: local
    domain-mode: custom
    subdomain: myapp
    components:
      - reference: frontend
        image: nginx:latest        # myapp.kind/
```

### Nice-URL Mode

Dot-separated URLs using registered domains. Requires `subdomain` and `base-domain`:

```yaml
deployments:
  - name: production
    cluster: odcn-production
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
        image: nginx:latest
        root: true                 # myapp.rijksapp.nl
      - reference: api
        image: myapi:latest        # api.myapp.rijksapp.nl
```

## Configuration Fields

### `base-domain`

Specifies a custom base domain for the deployment. Available on **any deployment**, not just nice-url mode.

- In `nice-url` mode: replaces the cluster domain with a registered domain (e.g., `rijksapp.nl`)
- In other modes: overrides the cluster's `ingress_postfix` for hostname resolution

The domain must be listed in the cluster's `nice_url.supported_domains` configuration.

```yaml
deployments:
  - name: production
    base-domain: rijksapp.dev      # Use this instead of cluster default domain
```

### `domain-format`

Configurable hostname pattern that controls which variables appear in the generated hostname. When set, hostnames are generated from the selected template instead of the default logic for the domain mode.

Available formats:

| Format ID | Dash variant | Dot variant |
|---|---|---|
| `component-deployment-project` | `frontend-poc-myapp.kind` | `frontend.poc.myapp.rijksapp.dev` |
| `component-deployment-subdomain` | `frontend-poc-moza.kind` | `frontend.poc.moza.rijksapp.dev` |
| `deployment-project` | `poc-myapp.kind` | `poc.myapp.rijksapp.dev` |
| `deployment-subdomain` | `poc-moza.kind` | `poc.moza.rijksapp.dev` |

- **Dash variant**: used for clusters without nice-URL support
- **Dot variant**: used when the cluster supports nice URLs (dot-separated hostnames)

The field is optional and backward-compatible. See [domain-format.md](../operations-manager/python/features/domain-format.md) for full details.

```yaml
deployments:
  - name: production
    domain-format: deployment-subdomain
    subdomain: myapp
    base-domain: rijksapp.dev
```

### `subdomain`

Specifies a custom subdomain used in hostname generation. Its behavior varies by domain mode:

- **`nice-url`**: used as the subdomain segment (e.g., `frontend.{subdomain}.{base-domain}`)
- **`custom`**: used as the entire hostname prefix
- **`deployment-name`**: implicitly uses the deployment name
- **`component-specific`**: not typically used

```yaml
deployments:
  - name: production
    subdomain: myapp
```

### `issuer`

Controls TLS certificate provisioning. When set to a Let's Encrypt value, a namespace-scoped `Issuer` resource is automatically generated.

| Value | Description |
|-------|-------------|
| `letsencrypt` | Let's Encrypt production ACME server |
| `letsencrypt-staging` | Let's Encrypt staging ACME server (for testing) |
| _(custom)_ | References an existing namespace-scoped Issuer |

Automatically set to `letsencrypt` when using `nice-url` mode with a `base-domain` in the create wizard. See [external-domains-letsencrypt.md](external-domains-letsencrypt.md) for full TLS integration details.

```yaml
deployments:
  - name: production
    issuer: letsencrypt
    base-domain: rijksapp.nl
```

### `root-component`

> **TODO**: The `root: true` marker is a domain/routing concern living on a component reference alongside unrelated fields like `image` and `imagePullPolicy`. It only applies in one specific combination (nice-url mode + format with `{component}`). A cleaner approach would express this as an explicit ingress/path configuration rather than a boolean flag on the component reference. To be revisited when the domain model is refactored.

In `nice-url` mode with formats that include `{component}` in the hostname, each component gets its own subdomain (e.g., `frontend.myapp.rijksapp.nl`). The root component additionally receives an ingress on the bare subdomain (e.g., `myapp.rijksapp.nl`).

Specified as `root: true` on a deployment component reference:

```yaml
deployments:
  - name: production
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    components:
      - reference: frontend
        image: nginx:latest
        root: true                 # Also serves myapp.rijksapp.nl
      - reference: api
        image: myapi:latest        # Only api.myapp.rijksapp.nl
```

When `domain-format` is set to a template without `{component}` (e.g., `deployment-subdomain`), all components share the same hostname and root component marking is skipped.

### Component `path`

Each component has a publication path (default `/`) controlling URL routing when components share a hostname (e.g., `deployment-name`, `custom` modes).

Supports both simple string and multi-path list format:

```yaml
components:
  - name: frontend
    path: "/"                      # Simple string format

  - name: api
    path:                          # Multi-path list format
      - match: /api
        rewrite: /
      - match: /health
```

Each path generates its own Kubernetes Ingress resource. The `rewrite` field is optional and strips the matched prefix before forwarding to the service.

Paths can also be overridden per deployment — see [Deployment-Level Paths](#deployment-level-paths).

### Deployment-Level Paths

Paths can be specified on deployment component references to override the component-level `path`. When present, deployment-level paths take precedence.

```yaml
components:
  - name: frontend
    path: "/"                      # Default path

deployments:
  - name: production
    components:
      - reference: frontend
        image: nginx:latest
        paths:                     # Overrides component-level path
          - match: /app
            rewrite: /
```

Fallback chain:
1. `deployments[].components[].paths` (deployment-level override)
2. `components[].path` (component-level default)
3. `[{"match": "/", "rewrite": null}]` (system default)

## Cluster Base Domains

Each cluster defines which domains it supports for nice URLs in `CLUSTER_CONFIG`:

| Cluster | Supported Domains |
|---------|-------------------|
| `local` | `kind`, `local` |
| `sandboxed-local` | `sandbox.rijksapp.dev`, `rijksapp.nl`, `rijksapp.dev` |
| `odcn-production` | `rijks.app`, `rijksapps.nl`, `rijksapp.nl`, `rijksapp.dev` |

The cluster's `ingress_postfix` (e.g., `.kind`, `.rig.prd1.gn2.quattro.rijksapps.nl`) is used as the default domain when no `base-domain` is specified.

## Complete YAML Reference

```yaml
name: my-project
display-name: My Application

components:
  - name: frontend
    path: "/"                          # Default path (simple string)
  - name: api
    path:                              # Multi-path (list format)
      - match: /api
        rewrite: /
      - match: /health

deployments:
  - name: production
    cluster: odcn-production
    namespace: my-namespace
    domain-mode: nice-url              # Required: one of 4 modes
    subdomain: myapp                   # Custom subdomain
    base-domain: rijksapp.nl           # Registered domain
    domain-format: deployment-subdomain # Optional: hostname template
    issuer: letsencrypt                # TLS certificate provisioning
    components:
      - reference: frontend
        image: nginx:latest
        root: true                     # Root component (nice-url only)
        paths:                         # Optional: override component paths
          - match: /app
            rewrite: /
      - reference: api
        image: myapi:latest
```

## Wizard Behavior

The create wizard produces a single "main" deployment. The domain step appears after the components step:

1. User selects a **domain mode** (defaults to `component-specific`)
2. For `nice-url` / `custom`: user provides a **subdomain**
3. For `nice-url`: user selects a **base domain** from cluster-supported options
4. For `nice-url`: user selects a **root component**
5. Optional: user selects a **domain format** template

On submit, the wizard:
- Assembles the deployment with `cluster`, `namespace`, `domain-mode`, `subdomain`, `base-domain`
- Maps `root-component` to `root: true` on the matching deployment component
- Auto-sets `issuer: letsencrypt` for `nice-url` with a base domain
