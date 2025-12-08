# PUBLIC_HOST Substitution in User Environment Variables

## Overview

The `PUBLIC_HOST` variable is automatically generated for components that have the `publish-on-web` service enabled. This variable contains the full public URL where the component is accessible.

Starting from this update, you can reference `PUBLIC_HOST` in your `user-env-vars` to build derived URLs.

## Protocol Based on Cluster Configuration

The protocol (http vs https) is determined by the cluster's TLS configuration:

| Cluster | TLS Enabled | Example PUBLIC_HOST |
|---------|-------------|---------------------|
| local | No | `http://api.kind` |
| odcn-production | Yes | `https://api.rig.prd1.gn2.quattro.rijksapps.nl` |

## Usage in user-env-vars

You can reference `PUBLIC_HOST` in your component's `user-env-vars` using either syntax:

### Simple syntax: `$PUBLIC_HOST`

```yaml
components:
  - name: api
    services:
      - publish-on-web
    user-env-vars:
      PUBLIC_API_URL: "$PUBLIC_HOST/api"
      CALLBACK_URL: "$PUBLIC_HOST/auth/callback"
```

### Braced syntax: `${PUBLIC_HOST}`

```yaml
components:
  - name: api
    services:
      - publish-on-web
    user-env-vars:
      PUBLIC_API_URL: "${PUBLIC_HOST}/api"
      FRONTEND_ORIGIN: "${PUBLIC_HOST}"
```

Both syntaxes are equivalent and can be mixed.

## Requirements

For `PUBLIC_HOST` substitution to work:

1. The component must have `publish-on-web` in its services
2. The variable must be referenced in `user-env-vars` (not in `aliases`)

## Example

```yaml
components:
  - name: backend
    port: 8080
    services:
      - publish-on-web
    user-env-vars:
      # These will be substituted with the actual PUBLIC_HOST value
      API_BASE_URL: "${PUBLIC_HOST}/api/v1"
      SWAGGER_URL: "${PUBLIC_HOST}/docs"
      CORS_ORIGIN: "$PUBLIC_HOST"

      # Static values (no substitution)
      LOG_LEVEL: "info"
      MAX_CONNECTIONS: "100"
```

### Result on local cluster:

```
API_BASE_URL=http://backend.kind/api/v1
SWAGGER_URL=http://backend.kind/docs
CORS_ORIGIN=http://backend.kind
LOG_LEVEL=info
MAX_CONNECTIONS=100
```

### Result on odcn-production cluster:

```
API_BASE_URL=https://backend.rig.prd1.gn2.quattro.rijksapps.nl/api/v1
SWAGGER_URL=https://backend.rig.prd1.gn2.quattro.rijksapps.nl/docs
CORS_ORIGIN=https://backend.rig.prd1.gn2.quattro.rijksapps.nl
LOG_LEVEL=info
MAX_CONNECTIONS=100
```

## Difference from Aliases

| Feature | user-env-vars with $PUBLIC_HOST | aliases |
|---------|--------------------------------|---------|
| Purpose | Reference PUBLIC_HOST in custom env vars | Reference service-provided secrets (database, minio, keycloak) |
| Supported variables | Only `PUBLIC_HOST` | All service secret variables |
| Storage | Component-specific secret | Service secrets (shared across components) |

For referencing database, minio, or keycloak variables, use `aliases` instead. See [ALIASES.md](./ALIASES.md) for details.

## Technical Notes

- Substitution happens at build time when creating deployment manifests
- Both `$PUBLIC_HOST` and `${PUBLIC_HOST}` patterns are supported
- If the component doesn't have `publish-on-web`, `PUBLIC_HOST` won't be available and no substitution occurs
- Future expansion to support more direct variables may be added by extending the alias system
