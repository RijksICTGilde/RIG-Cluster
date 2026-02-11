# Keycloak Domain Migration

## Overview

The production Keycloak hostname was migrated from `keycloak.rig.prd1.gn2.quattro.rijksapps.nl` to `keycloak.rijksapp.nl`. Both hostnames remain active during a transition period.

## How It Works

### Dual-Ingress Setup

Two Kubernetes Ingress resources serve the same Keycloak deployment:

- **Primary** (`keycloak`): `keycloak.rijksapp.nl` with Let's Encrypt TLS certificate
- **Legacy** (`keycloak-legacy`): `keycloak.rig.prd1.gn2.quattro.rijksapps.nl` with default OpenShift certificate

The `KC_HOSTNAME` environment variable is set to `https://keycloak.rijksapp.nl`, which controls:
- The `iss` (issuer) claim in all new JWT tokens
- The OIDC discovery document's `issuer` field
- The Keycloak admin console URL

### Token Validation

OIDC clients validate tokens by comparing the `iss` claim against the `issuer` field from the discovery document. Since both are derived from `KC_HOSTNAME`, they always match regardless of which hostname the client used to reach Keycloak.

`KC_HOSTNAME_STRICT=false` ensures Keycloak accepts requests on any hostname.

### Application Secrets

When OPI processes a project, the Keycloak discovery URL written into application secrets is derived from `cluster_config.py`:

```python
"keycloak_discovery_url": "https://keycloak.rijksapp.nl"
```

Existing application secrets keep the old hostname until the project is reprocessed. This is safe because the legacy ingress continues to serve the old hostname.

## Impact on Existing Applications

| Scenario | Impact |
|----------|--------|
| Existing tokens | Valid until expiry (Keycloak validates by signing key, not issuer) |
| New tokens | Issued with `iss: https://keycloak.rijksapp.nl/realms/...` |
| Old discovery URLs | Still resolve via legacy ingress |
| Project reprocessing | New secrets use `keycloak.rijksapp.nl` |

## SAML SP Entity ID

The `sso-rijk-direct` SAML IDP (currently disabled) uses `saml_sp_entity_id` derived from `settings.KEYCLOAK_URL`. When activating direct SSO-Rijk SAML integration:

1. Register the new entity ID with SSO-Rijk: `https://keycloak.rijksapp.nl/realms/rig-platform`
2. Update `KEYCLOAK_URL` in the production configmap to the new hostname (already done)

## Removing the Legacy Ingress

The legacy ingress can be removed once:

1. All project secrets have been updated (reprocess all projects)
2. No external systems reference the old hostname
3. SSO-Rijk registration (if activated) uses the new hostname

Files to remove:
- `infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn/ingress-legacy.yaml`
- Remove the resource reference from `overlays/odcn/kustomization.yaml`

## Files Changed

| File | Change |
|------|--------|
| `keycloak/controller/overlays/odcn/kustomization.yaml` | `KC_HOSTNAME` and main ingress use `keycloak.rijksapp.nl` |
| `keycloak/controller/overlays/odcn/ingress-legacy.yaml` | Legacy ingress for old hostname (was `ingress-rijksapp.yaml`) |
| `opi/core/cluster_config.py` | `keycloak_discovery_url` updated for `odcn-production` |
| `operations-manager/overlays/odcn-production/configmap.yaml` | `KEYCLOAK_URL` and `OIDC_DISCOVERY_URL` updated |
| Various bootstrap YAML comments | Updated hostname references |
