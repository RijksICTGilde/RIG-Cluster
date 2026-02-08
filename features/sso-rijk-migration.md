# SSO-Rijk Direct SAML Migration

This feature enables migrating from OIDC-based SSO-Rijk authentication (via Digilab) to direct SAML authentication with SSO-Rijk.

## What It Is

The RIG Platform currently authenticates users through a chain:
```
User → RIG Keycloak → Digilab Keycloak (OIDC) → SSO-Rijk (SAML)
```

This feature removes the Digilab intermediary:
```
User → RIG Keycloak → SSO-Rijk (SAML)
```

Benefits:
- Direct connection to SSO-Rijk (no intermediary dependency)
- Reduced latency in authentication flow
- Simplified architecture

## Prerequisites

1. **SSO-Rijk SP Registration**: Register the RIG Platform Keycloak as a Service Provider with SSO-Rijk:
   - Entity ID: `https://keycloak.rijksapp.nl/realms/rig-platform`
   - ACS URL: `https://keycloak.rijksapp.nl/realms/rig-platform/broker/sso-rijk/endpoint`

2. **Bootstrap Configuration**: Both IDPs must exist in Keycloak (created by bootstrap)

## Configuration

### Bootstrap YAML (`bootstrap.yaml`)

The bootstrap creates two identity providers:

**OIDC IDP (current, via Digilab)**:
```yaml
identityProviders:
  - alias: "sso-rijk"
    providerId: "oidc"
    enabled: true
    authenticateByDefault: true
    config:
      clientId: "{{ sso_client_id }}"
      clientSecret: "{{ sso_client_secret }}"
      discoveryUrl: "{{ sso_discovery_url }}"
```

**SAML IDP (future, direct SSO-Rijk)**:
```yaml
  - alias: "sso-rijk-direct"
    providerId: "saml"
    enabled: false  # Disabled until SSO-Rijk registration complete
    config:
      idpEntityId: "https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/metadata"
      singleSignOnServiceUrl: "https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/single-sign-on"
      entityId: "{{ saml_sp_entity_id }}"
      # ... full SAML configuration
```

### SAML Mappers

The SAML IDP requires different mappers than OIDC to extract attributes from the SAML response:

| Attribute | SAML Mapper Type | Source |
|-----------|------------------|--------|
| `sso-rijk-userid` | XPath | NameID |
| `sso-rijk-userid-lowercase` | XPath + LOWERCASE | NameID |
| `email` | Attribute | `urn:rijksoverheid:federation:emailAddress` |
| `firstName` | Attribute | `urn:rijksoverheid:federation:givenName` |
| `lastName` | Attribute | `urn:rijksoverheid:federation:surName` |
| `organizationName` | Attribute | `urn:rijksoverheid:federation:organizationDisplayName` |
| `organizationNumber` | Attribute | `urn:rijksoverheid:federation:organizationNumber` |

## Migration Process

### Step 1: Verify Prerequisites

```bash
# Check both IDPs exist
python migrate_sso_idp.py status \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <PASSWORD>
```

### Step 2: Dry Run

```bash
# Port-forward to database
kubectl port-forward svc/rig-db-rw -n rig-system 5432:5432

# Get credentials
KEYCLOAK_PWD=$(kubectl get secret -n rig-system keycloak-credentials -o jsonpath='{.data.admin-password}' | base64 -d)
DB_PWD=$(kubectl get secret -n rig-system keycloak-db-credentials -o jsonpath='{.data.password}' | base64 -d)

# Dry run
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password "$KEYCLOAK_PWD" \
  --old-alias sso-rijk \
  --new-alias sso-rijk-direct \
  --db-password "$DB_PWD" \
  --bootstrap-yaml /path/to/bootstrap.yaml \
  --dry-run
```

### Step 3: Execute Migration

```bash
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password "$KEYCLOAK_PWD" \
  --old-alias sso-rijk \
  --new-alias sso-rijk-direct \
  --db-password "$DB_PWD" \
  --bootstrap-yaml /path/to/bootstrap.yaml
```

This performs a **safe swap**:
1. Renames `sso-rijk` (OIDC) to `sso-rijk-obsolete` (disabled, kept as fallback)
2. Renames `sso-rijk-direct` (SAML) to `sso-rijk` (enabled, takes over)
3. Updates `bootstrap.yaml` to match the new state
4. Preserves all user federated identities (they reference the alias)

### Step 4: Commit and Restart

```bash
# Commit updated bootstrap.yaml
git add bootstrap.yaml
git commit -m "Migrate SSO-Rijk from OIDC to direct SAML"

# Restart Keycloak to clear caches
kubectl rollout restart deployment/keycloak -n rig-system
```

### Step 5: Verify

1. Test login with an existing user
2. Verify user attributes are populated correctly
3. Check no new user accounts were created

## How It Works

### User Identity Preservation

The migration preserves user accounts because:

1. **Federated identities reference the alias**: The `federated_identity` table stores `identity_provider = 'sso-rijk'` (the alias)
2. **User ID matches**: Both IDPs use the SSO-Rijk NameID (`urn:collab:person:minbzk.nl:Username`) as the user identifier
3. **Alias takeover**: The SAML IDP takes over the `sso-rijk` alias

```
Before: federated_identity.identity_provider = 'sso-rijk' → OIDC IDP
After:  federated_identity.identity_provider = 'sso-rijk' → SAML IDP (same user ID)
```

### Authentication Flow Continuity

The authentication flow configuration doesn't change:
```yaml
authenticationFlows:
  - executions:
      - authenticator: "identity-provider-redirector"
        config:
          defaultProvider: "sso-rijk"  # Still points to 'sso-rijk' alias
```

## Rollback

The old IDP is preserved as `sso-rijk-obsolete`. To rollback:

1. Restore database from backup (created automatically)
2. Restore `bootstrap.yaml` from git
3. Restart Keycloak

## Troubleshooting

### "Account already exists" Error

The federated identity didn't match. Check:
```sql
SELECT federated_user_id FROM federated_identity
WHERE identity_provider = 'sso-rijk';
```
Compare with the NameID from the SSO-Rijk SAML response.

### SAML Signature Validation Fails

The SSO-Rijk signing certificate may have changed. Update the certificate in `bootstrap.yaml` from SSO-Rijk metadata.

### User Attributes Missing

Verify SAML mappers are correctly configured. Check:
```bash
python migrate_sso_idp.py status --admin-password <PASSWORD>
```

## Related Features

- [keycloak-yaml-templates.md](keycloak-yaml-templates.md) - Bootstrap YAML configuration
- [local-cluster-federation.md](local-cluster-federation.md) - Local cluster using production Keycloak

## Files

- `operations-manager/python/opi/configs/keycloak/bootstrap.yaml` - Bootstrap configuration
- `operations-manager/python/opi/connectors/keycloak.py` - SAML IDP support
- `operations-manager/python/opi/handlers/keycloak_yaml_handler.py` - YAML processing
- `keycloak-migration/sso-migration/migrate_sso_idp.py` - Migration script
