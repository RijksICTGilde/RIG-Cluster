# Local Cluster Federation

This feature enables local Kind clusters to authenticate users via the production RIG Platform Keycloak, providing real SSO-Rijk authentication during local development.

## What It Is

Local Kind clusters can use the production Keycloak as an upstream identity provider:

```
Local User
    → Local Keycloak (Kind)
    → Production Keycloak (rig-platform)
    → SSO-Rijk
```

Benefits:
- Real SSO-Rijk authentication in local development
- Same user identity across local and production
- Test with actual user attributes and roles
- No need to mock authentication

## Prerequisites

1. **Production Keycloak Running**: The RIG Platform Keycloak must be accessible
2. **Client Created**: The `local-cluster` client must exist in production (created by bootstrap)
3. **Client Secret**: Obtain the client secret from production Keycloak

## Configuration

### Production Keycloak (`bootstrap.yaml`)

The production bootstrap creates a client for local clusters:

```yaml
clients:
  - clientId: "local-cluster"
    name: "Local Kind Cluster"
    enabled: true
    publicClient: false
    redirectUris:
      - "https://keycloak.kind/*"
      - "http://keycloak.kind/*"
      - "https://localhost:*/*"
    defaultClientScopes:
      - "custom_attributes_passthrough"  # Passes through SSO-Rijk attributes
```

### Local Cluster (`bootstrap-local.yaml`)

The local bootstrap configures the Kind cluster to use production as upstream:

```yaml
identityProviders:
  - alias: "sso-rijk"
    displayName: "SSO Rijk (via RIG Platform)"
    providerId: "oidc"
    enabled: true
    authenticateByDefault: true
    config:
      clientId: "{{ sso_client_id }}"      # local-cluster
      clientSecret: "{{ sso_client_secret }}" # from production
      discoveryUrl: "{{ sso_discovery_url }}" # production Keycloak
```

## How to Use

### Step 1: Get Client Secret from Production

```bash
# Option A: From Keycloak Admin Console
# Navigate to: rig-platform realm → Clients → local-cluster → Credentials

# Option B: From Kubernetes Secret (after bootstrap)
kubectl get secret keycloak-client-local-cluster -n rig-system \
  -o jsonpath='{.data.client-secret}' | base64 -d
```

### Step 2: Configure Local Environment

Set these environment variables before starting the local operations-manager:

```bash
# Select local bootstrap configuration
export KEYCLOAK_BOOTSTRAP_CONFIG=local

# Point to production Keycloak
export KEYCLOAK_MASTER_OIDC_CLIENT_ID=local-cluster
export KEYCLOAK_MASTER_OIDC_CLIENT_SECRET=<secret from step 1>
export KEYCLOAK_MASTER_OIDC_DISCOVERY_URL=https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform/.well-known/openid-configuration
```

### Step 3: Start Local Cluster

```bash
# Start Kind cluster with operations-manager
task cluster:up

# The bootstrap will use bootstrap-local.yaml and configure the OIDC IDP
```

### Step 4: Test Authentication

1. Access a local application that uses Keycloak
2. You'll be redirected to production Keycloak → SSO-Rijk
3. After login, you return to the local application with full user attributes

## Attribute Passthrough

All SSO-Rijk attributes are passed through the chain:

| Attribute | Description |
|-----------|-------------|
| `sso-rijk-userid` | Original SSO-Rijk NameID (`urn:collab:person:...`) |
| `sso-rijk-userid-lowercase` | Lowercase version for username matching |
| `email` | User's email address |
| `firstName`, `lastName` | User's name |
| `organization.name` | Organization display name |
| `organization.number` | Organization OIN number |

These are mapped through the `custom_attributes_passthrough` client scope, which:
1. Production Keycloak receives from SSO-Rijk
2. Passes to local Keycloak via OIDC claims
3. Local Keycloak stores as user attributes
4. Passes to local applications via tokens

## Configuration Reference

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `KEYCLOAK_BOOTSTRAP_CONFIG` | Bootstrap config type | `local` |
| `KEYCLOAK_MASTER_OIDC_CLIENT_ID` | Client ID for upstream | `local-cluster` |
| `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET` | Client secret | `<from production>` |
| `KEYCLOAK_MASTER_OIDC_DISCOVERY_URL` | OIDC discovery URL | `https://keycloak.../realms/rig-platform/.well-known/openid-configuration` |

### Bootstrap Config Values

| Value | Bootstrap File | Use Case |
|-------|----------------|----------|
| `default` | `bootstrap.yaml` | Production with direct SSO-Rijk |
| `local` | `bootstrap-local.yaml` | Local with production upstream |

## Troubleshooting

### Redirect URI Mismatch

```
Invalid redirect_uri
```

**Solution**: Ensure your local Keycloak URL matches the redirect URIs configured in the `local-cluster` client:
- `https://keycloak.kind/*`
- `http://keycloak.kind/*`

### Client Not Found

```
Client not found: local-cluster
```

**Solution**: Deploy/refresh the production Keycloak to create the `local-cluster` client.

### Attributes Missing

User logs in but attributes are empty.

**Solution**:
1. Verify `custom_attributes_passthrough` scope is assigned to `local-cluster` client
2. Check production Keycloak has the user's attributes populated
3. Verify mapper configuration in `bootstrap-local.yaml`

### Certificate Errors

```
SSL certificate problem: unable to get local issuer certificate
```

**Solution**: For local development, you may need to trust the production Keycloak certificate or disable verification (not recommended for production).

## Security Considerations

- The `local-cluster` client secret should be kept secure
- Local development traffic goes to production Keycloak
- Production user sessions are created during local testing
- Consider using a separate "development" realm for isolation

## Related Features

- [sso-rijk-migration.md](sso-rijk-migration.md) - Direct SSO-Rijk SAML migration
- [keycloak-yaml-templates.md](keycloak-yaml-templates.md) - Bootstrap YAML configuration
- [keycloak-external-provider.md](keycloak-external-provider.md) - External Keycloak for projects

## Files

- `operations-manager/python/opi/configs/keycloak/bootstrap.yaml` - Production bootstrap (creates `local-cluster` client)
- `operations-manager/python/opi/configs/keycloak/bootstrap-local.yaml` - Local bootstrap (uses production upstream)
- `operations-manager/python/opi/core/config.py` - `KEYCLOAK_BOOTSTRAP_CONFIG` setting
- `operations-manager/python/opi/bootstrap/keycloak_setup.py` - Bootstrap file selection logic
