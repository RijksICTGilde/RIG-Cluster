# Additional Keycloak Clients

This feature allows a project to create OIDC clients in its realm for other projects, enabling shared authentication.

## What It Is

When a project owns a Keycloak realm, it can create additional OIDC clients for other projects that will share the same realm. This is the counterpart to the [external keycloak provider](keycloak-external-provider.md) feature.

The owner project:
- Manages the realm and its configuration
- Creates clients for itself AND other projects
- Stores additional client credentials as Kubernetes secrets

## When to Use

Use additional clients when:
- Your project's realm should be shared with other applications
- You want unified SSO across multiple apps
- You need to manage access control for all apps from one place

## Configuration

### Owner Project Configuration

```yaml
# mb-docs-helmfile.yaml
services:
- keycloak:
    config:
      template: sso-support
      additional-clients:
        - name: mb-grist-helmfile-production
          redirect-uris:
            - https://grist.rijksapp.nl/*
            - https://grist.rijksapp.nl/oauth2/callback
        - name: mb-another-app-production
          redirect-uris:
            - https://another.rijksapp.nl/*
```

### Configuration Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Client ID for the additional client |
| `redirect-uris` | No | List of allowed redirect URIs (defaults to `["*"]`) |

## How It Works

1. **Realm creation/update**: When the owner project is deployed, it creates/updates its realm.

2. **Additional clients created**: After the main client, additional clients are created in the same realm.

3. **Secrets stored in Kubernetes**: Each additional client's credentials are stored as a separate Kubernetes secret in the operations namespace.

4. **Other projects use the secret**: Dependent projects with `type: external` read these secrets.

## Secret Naming Convention

Additional client secrets are named using the pattern:
```
keycloak-client-{normalized-client-name}
```

Where `normalized-client-name` is:
- Lowercased
- Underscores replaced with hyphens

**Example:**
- Client name: `mb-grist-helmfile-production`
- Secret name: `keycloak-client-mb-grist-helmfile-production`

## Secret Structure

Created secrets contain all information needed by the dependent project:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-client-mb-grist-helmfile-production
  namespace: rig-prd-operations
  labels:
    app.kubernetes.io/managed-by: operations-manager
    app.kubernetes.io/component: keycloak-client
type: Opaque
data:
  client-id: <base64>
  client-secret: <base64>
  realm: <base64>
  host: <base64>
  discovery-url: <base64>
```

## Idempotent Operation

Creating additional clients is idempotent:
- If the client already exists, it updates the configuration
- If the secret already exists, it updates the secret data
- Re-running a deployment doesn't create duplicates

## Example: Complete Setup

### Owner Project (docs)

```yaml
# mb-docs-helmfile.yaml
services:
- keycloak:
    config:
      template: sso-support
      # Create realm roles for unified access control
      realm-roles:
        - name: allowed-user
          description: Access to all MijnBureau applications
      # Create clients for other projects
      additional-clients:
        - name: mb-grist-helmfile-production
          redirect-uris:
            - https://grist.rijksapp.nl/*
        - name: mb-nextcloud-helmfile-production
          redirect-uris:
            - https://nextcloud.rijksapp.nl/*
            - https://nextcloud.rijksapp.nl/apps/user_oidc/code
      # Restrict access using realm role
      restrict_access:
        enabled: true
        realm-role: allowed-user
```

### Dependent Project (grist)

```yaml
# mb-grist-helmfile.yaml
services:
- keycloak:
    type: external
    config:
      host: https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl
      realm: mb-docs-helmfile-odcn-production
      client-id: mb-grist-helmfile-production
      client-secret: <AGE encrypted client secret>
```

### Dependent Project (nextcloud)

```yaml
# mb-nextcloud-helmfile.yaml
services:
- keycloak:
    type: external
    config:
      host: https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl
      realm: mb-docs-helmfile-odcn-production
      client-id: mb-nextcloud-helmfile-production
      client-secret: <AGE encrypted client secret>
```

## Verifying Client Creation

After deploying the owner project, verify additional clients were created:

```bash
# Check the Kubernetes secrets
kubectl get secrets -n rig-prd-operations | grep keycloak-client

# View secret contents (base64 decoded)
kubectl get secret keycloak-client-mb-grist-helmfile-production -n rig-prd-operations -o jsonpath='{.data.client-id}' | base64 -d
```

## Redirect URIs

When specifying redirect URIs:
- Use wildcards (`*`) carefully for development convenience
- Be specific in production for security
- Include all callback paths your application uses

**Common patterns:**
```yaml
redirect-uris:
  - https://app.example.com/*                    # All paths
  - https://app.example.com/oauth/callback       # Specific callback
  - https://app.example.com/api/auth/callback/*  # Auth paths
```

## Troubleshooting

### Client Not Created

Check the operations-manager logs for errors:
```bash
kubectl logs -n rig-prd-operations deployment/operations-manager | grep "additional client"
```

### Secret Not Accessible

Ensure the dependent project is looking in the correct namespace. The operations namespace is cluster-specific:
- `local`: `rig-system`
- `odcn-production`: `rig-prd-operations`

## Related Features

- [keycloak-external-provider.md](keycloak-external-provider.md) - Using credentials from another project
- [keycloak-realm-roles.md](keycloak-realm-roles.md) - Realm-level roles for unified access
- [keycloak-yaml-templates.md](keycloak-yaml-templates.md) - Keycloak configuration templates
