# Keycloak Redirect URI Configuration

## Overview

Configure custom redirect URIs for Keycloak clients, such as localhost URLs for local development.

## How to Use

### Adding Localhost URLs for Development

Add `additional_redirect_uris` to your project's Keycloak configuration:

```yaml
name: my-project

services:
  - keycloak:
      config:
        template: "algoritmeregister"  # or "sso-only", etc.
        additional_redirect_uris:
          - "http://localhost:3000/*"
          - "http://127.0.0.1:3000/*"
          - "http://localhost:8080/*"
          - "http://127.0.0.1:8080/*"

deployments:
  - name: development
    cluster: local
    # ...
```

### Configuration Options

- `additional_redirect_uris` - List of additional redirect URIs
  - Must be a YAML list
  - Each URI should include protocol and `/*` wildcard suffix
  - Commonly used for localhost URLs during local development

### Protocol Support

**Local clusters** (e.g., `kind`):
- Generates both HTTP and HTTPS redirect URIs for ingress hosts
- Example: `http://myapp.kind/*` AND `https://myapp.kind/*`

**Production clusters**:
- Generates only HTTPS redirect URIs for ingress hosts
- Example: `https://myapp.example.com/*`

## Examples

### Local Development with Frontend on Port 3000

```yaml
services:
  - keycloak:
      config:
        template: "sso-only"
        additional_redirect_uris:
          - "http://localhost:3000/*"
          - "http://127.0.0.1:3000/*"
```

### Multiple Development Servers

```yaml
services:
  - keycloak:
      config:
        template: "algoritmeregister"
        additional_redirect_uris:
          - "http://localhost:3000/*"   # Frontend
          - "http://127.0.0.1:3000/*"
          - "http://localhost:8080/*"   # Backend
          - "http://127.0.0.1:8080/*"
```

### Production (No Localhost)

```yaml
services:
  - keycloak:
      config:
        template: "sso-only"
        # No additional_redirect_uris needed
```

## Troubleshooting

**Issue**: Authentication redirect fails from localhost

**Solution**: Add your localhost URL to `additional_redirect_uris`:
```yaml
additional_redirect_uris:
  - "http://localhost:YOUR_PORT/*"
```

**Issue**: "additional_redirect_uris must be a list" error

**Solution**: Use YAML list format:
```yaml
# Correct
additional_redirect_uris:
  - "http://localhost:3000/*"

# Incorrect
additional_redirect_uris: "http://localhost:3000/*"
```
