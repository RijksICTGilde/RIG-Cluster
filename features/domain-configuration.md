# Domain Configuration

## Overview

Domain configuration controls how project components are accessible via URLs. This configuration is **per-deployment**, not project-level, because each deployment (e.g., production, staging, feature branches) may have different URL requirements.

## Domain Modes

The wizard supports four domain modes:

| Mode | Description | URL Example |
|------|-------------|-------------|
| `component-specific` | Each component gets a unique URL based on its name | `frontend-myproject.cluster.example.com` |
| `deployment-name` | All components share the deployment name as subdomain | `production.cluster.example.com/frontend` |
| `custom` | User specifies a custom subdomain | `myapp.cluster.example.com` |
| `nice-url` | Dot-separated URLs with a registered domain | `frontend.myapp.rijks.app` |

### Nice URL Mode

The `nice-url` mode requires:

- A **subdomain** (e.g., `myapp`)
- A **base domain** from the cluster's supported domains (e.g., `rijksapp.nl`)
- A **root component** that responds on the base URL (e.g., `myapp.rijks.app`)

The base domain options are derived from `CLUSTER_CONFIG[cluster].nice_url.supported_domains`.

## Wizard Behavior

### Create Project Wizard

The wizard creates a single "main" deployment. The domain step appears after the components step and seeds:

```yaml
deployments:
  - name: main
    domain-mode: component-specific  # default
```

On submit, the wizard assembles the full deployment structure including:

- `cluster` and `namespace` from the identity step
- `domain-mode`, `subdomain`, `base-domain` from the domain step
- Component references from the components step
- `root-component` mapped to `root: true` on the matching deployment component
- `issuer: letsencrypt` auto-set when using `nice-url` with a base domain

### Component Path

Each component has a **publication path** (default `/`) that determines URL routing when using shared domains (`deployment-name`, `custom`). For example:

- `frontend` with path `/` serves the root
- `api` with path `/api` serves the API endpoints

## Current Architecture

Domain configuration lives on deployments, not on projects. This means:

1. Each deployment independently defines its URL strategy
2. The wizard's domain step configures the initial "main" deployment
3. Additional deployments (created via API or cloned) inherit or override these settings

## Future Vision

### Project-Level Domain Defaults

A potential enhancement would add project-level domain defaults that deployments inherit:

```yaml
# Future: project-level domain defaults
domain-defaults:
  mode: nice-url
  subdomain: myapp
  base-domain: rijksapp.nl
  root-component: frontend
```

Deployments could then:

- **Inherit** the project defaults (most common case)
- **Override** specific fields (e.g., different subdomain for staging)
- **Use a different mode entirely** (e.g., feature branches use `deployment-name` while production uses `nice-url`)

### Mixed-Mode Deployments

A real-world scenario: the main production deployment uses `nice-url` with a registered domain (`myapp.rijks.app`), while feature branch deployments automatically use `deployment-name` mode for ephemeral URLs (`feature-123.cluster.example.com`).

This would require the project to specify:

- Default domain mode for "primary" deployments
- Fallback domain mode for "secondary" / ephemeral deployments
- Rules for which deployments are primary vs secondary

### Subdomain Registration

The existing `/subdomains/check` endpoint can verify subdomain availability before committing to a nice-url configuration. Integration with the wizard for real-time validation is a future improvement.
