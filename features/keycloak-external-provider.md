# External Keycloak Provider

This feature allows a project to use a Keycloak realm managed by another project, enabling shared authentication across multiple applications.

## What It Is

When multiple projects need to share a single Keycloak realm (for unified SSO experience), one project owns the realm and creates clients for other projects. The dependent projects use `type: external` to reference the pre-created client credentials instead of creating their own realm.

This enables:
- Single Sign-On (SSO) between multiple applications
- Unified user management across apps
- Shared realm roles for access control

## When to Use

Use external keycloak when:
- Multiple applications should share the same authentication realm
- Users should have SSO between apps (single login for all)
- A unified role grants access to multiple applications

Do NOT use external keycloak when:
- The application needs its own isolated user base
- Different applications need different identity providers

## Configuration

### Dependent Project Configuration

The project using an external keycloak provider specifies all credentials inline:

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

### Configuration Fields

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | Must be `external` to use external provider |
| `config.host` | Yes | Keycloak base URL |
| `config.realm` | Yes | Realm name (from the owner project) |
| `config.client-id` | Yes | OIDC client ID (created by owner project) |
| `config.client-secret` | Yes | OIDC client secret (can be AGE encrypted) |

The `discovery-url` is automatically derived from `host` and `realm`.

## How It Works

1. **Owner project creates the client**: The project owning the realm uses `additional-clients` to create a client for the dependent project (see [keycloak-additional-clients.md](keycloak-additional-clients.md)).

2. **Secret is stored in Kubernetes**: When the owner project is refreshed, the additional client's credentials are stored as a Kubernetes secret for reference.

3. **Get the client secret**: After deploying the owner project, retrieve the client secret from the K8s secret or Keycloak admin console.

4. **Configure dependent project**: Add the credentials inline in the dependent project's config (AGE encrypt the client-secret).

5. **Deployment secret created**: When the dependent project is deployed, it creates the deployment's keycloak secret with the same structure as a normal keycloak setup.

## Credential Flow

```
[Owner Project] --creates--> [Realm + Client] --stores--> [K8s Secret]
                                                              |
                                                     (manual copy)
                                                              |
                                                              v
[Dependent Project File] <-- inline config with client-secret (AGE encrypted)
         |
         v
[Deployment Secret] --mounts--> [Deployment Pods] --uses--> [OIDC Auth]
```

## Example: MijnBureau Shared Realm

### Docs Project (Owner)

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
      realm-roles:
        - name: allowed-user
          description: Access to MijnBureau applications
      restrict-access:
        enabled: true
        realm-role: allowed-user
```

### Grist Project (Dependent)

```yaml
# mb-grist-helmfile.yaml
services:
- keycloak:
    type: external
    config:
      host: https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl
      realm: mb-docs-helmfile-odcn-production
      client-id: mb-grist-helmfile-production
      client-secret: |-
        -----BEGIN AGE ENCRYPTED FILE-----
        <encrypted client secret from docs project>
        -----END AGE ENCRYPTED FILE-----
```

## Order of Operations

1. **Deploy owner project first**: The owner project must be deployed/refreshed first to create the realm and additional clients.

2. **Deploy dependent project second**: After the Kubernetes secret exists, the dependent project can be deployed.

3. **Redeploy if secret missing**: If the dependent project is deployed before the owner creates the secret, it will fail. Redeploy after the owner project is ready.

## Troubleshooting

### Missing Required Field Error

```
External keycloak config missing required field: 'client-secret'
```

**Solution**: Ensure all required fields are present in the config:
- `host`
- `realm`
- `client-id`
- `client-secret`

### Client Secret Not Yet Available

If the owner project hasn't been deployed yet, the client secret won't exist.

**Solution**:
1. Deploy the owner project first
2. Get the client secret:
   ```bash
   kubectl get secret keycloak-client-mb-grist-helmfile-production -n rig-prd-operations -o jsonpath='{.data.client-secret}' | base64 -d
   ```
3. AGE encrypt the secret and add to the dependent project's config
4. Deploy the dependent project

### Decryption Error

If the client-secret is AGE encrypted but decryption fails:

**Solution**: Ensure the project's age-private-key can decrypt the client-secret. The secret should be encrypted with the project's age-public-key.

## Related Features

- [keycloak-additional-clients.md](keycloak-additional-clients.md) - Creating clients for other projects
- [keycloak-realm-roles.md](keycloak-realm-roles.md) - Realm-level roles for unified access
- [client-access-restriction.md](client-access-restriction.md) - Restricting access to users with specific roles
