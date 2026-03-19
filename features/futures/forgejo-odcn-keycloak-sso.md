# Forgejo on ODCN with Keycloak SSO

**Status**: Planned

## Overview

Deploy Forgejo as an in-cluster git server on the ODCN production cluster, authenticated via the existing Keycloak instance using OpenID Connect (OIDC). This gives developers a web-based git platform for browsing repositories while also serving as a git backend for ArgoCD and the Operations Manager.

## Why

- **Self-hosted git**: Reduce dependency on external GitHub for cluster configuration repositories
- **SSO via Keycloak**: Developers log in with their existing SSO-Rijk credentials - no separate accounts
- **Consistent with sandbox**: The sandboxed-local cluster already runs Forgejo; ODCN production would follow the same pattern
- **GitOps backend**: ArgoCD and Operations Manager can push/pull from an in-cluster Forgejo instead of (or in addition to) GitHub

## Architecture

```
Developer browser
  → Forgejo (https://forgejo.<odcn-domain>)
    → Keycloak OIDC (https://keycloak.rijksapp.nl / rig-platform realm)
      → SSO-Rijk

ArgoCD / Operations Manager
  → Forgejo (http://forgejo.rig-prd-operations:3000) via service account + API token
```

### Authentication Flow

1. User navigates to Forgejo
2. Forgejo redirects to Keycloak OIDC authorization endpoint
3. Keycloak redirects to SSO-Rijk (SAML)
4. User authenticates with government credentials
5. SSO-Rijk returns SAML assertion to Keycloak
6. Keycloak issues OIDC tokens to Forgejo
7. Forgejo creates/updates local user from OIDC claims (email, name, organization)
8. User is logged in

### Service Accounts

ArgoCD and Operations Manager do not use SSO. They authenticate via:
- A bootstrap admin account (`rig-admin`) with an API token
- Or dedicated machine-user accounts created during bootstrap

## Implementation Plan

### 1. Keycloak Client for Forgejo

Add a `forgejo` client to the `rig-platform` realm via the existing YAML template system.

**New file**: `operations-manager/python/opi/configs/keycloak/forgejo.yaml`

```yaml
# Forgejo OIDC Client in the platform realm
# Processed during Operations Manager bootstrap

clients:
  - clientId: "forgejo"
    name: "Forgejo Git Server"
    enabled: true
    publicClient: false
    protocol: "openid-connect"
    redirectUris:
      - "https://forgejo.{{ domain }}/*"
    webOrigins:
      - "https://forgejo.{{ domain }}"
    standardFlowEnabled: true
    directAccessGrantsEnabled: false
    serviceAccountsEnabled: false
    defaultClientScopes:
      - "custom_attributes_passthrough"
```

This follows the same pattern as the existing `development-clusters` client in `bootstrap.yaml`. The Operations Manager processes it during startup/bootstrap using the `KeycloakConnector`.

### 2. ODCN Kustomize Overlays

Create overlays under the existing Forgejo infrastructure path.

**Controller overlay**: `infrastructure/bootstrap/infrastructure/forgejo/controller/overlays/odcn-production/`

| File | Purpose |
|------|---------|
| `kustomization.yaml` | Patches for ODCN: namespace, image, domain, database |
| `statefulset-patch.yaml` | Rootless image, ODCN domain, OIDC env vars, database credentials |
| `ingress-patch.yaml` | ODCN domain with TLS (cert-manager or HAProxy termination) |

**Config overlay**: `infrastructure/bootstrap/infrastructure/forgejo/config/overlays/odcn-production/`

| File | Purpose |
|------|---------|
| `kustomization.yaml` | Bootstrap job patches |
| `bootstrap-job-patch.yaml` | ODCN-specific repos, credentials, rootless paths |

### 3. Forgejo OIDC Configuration

Forgejo supports OIDC via environment variables in the StatefulSet:

```yaml
# Enable OAuth2 provider support
- name: FORGEJO__oauth2__ENABLED
  value: "true"

# Require external registration (force SSO, no local signup)
- name: FORGEJO__service__ALLOW_ONLY_EXTERNAL_REGISTRATION
  value: "true"

# Allow registration so OIDC users get accounts on first login
- name: FORGEJO__service__DISABLE_REGISTRATION
  value: "false"

# Auto-discover email from OIDC claims
- name: FORGEJO__service__DEFAULT_ALLOW_CREATE_ORGANIZATION
  value: "false"

# Require sign-in to view anything (private instance)
- name: FORGEJO__service__REQUIRE_SIGNIN_VIEW
  value: "true"
```

The OIDC auth source itself must be registered via the Forgejo Admin API during bootstrap (not configurable via `app.ini` env vars). The bootstrap Job calls:

```bash
# Register Keycloak as OIDC authentication source
curl -X POST "http://localhost:3000/api/v1/admin/identity-sources" \
  -H "Authorization: token ${FORGEJO_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "oauth2",
    "name": "keycloak",
    "oauth2": {
      "provider": "openidConnect",
      "clientID": "forgejo",
      "clientSecret": "'${FORGEJO_OIDC_CLIENT_SECRET}'",
      "openIDConnectAutoDiscoveryURL": "https://keycloak.rijksapp.nl/realms/rig-platform/.well-known/openid-configuration",
      "scopes": ["openid", "profile", "email"],
      "requiredClaimName": "",
      "groupClaimName": "",
      "adminGroup": "",
      "restrictedGroup": ""
    }
  }'
```

Alternatively, this can be done via the `forgejo admin auth add-oauth` CLI command in the bootstrap Job.

### 4. Operations Manager Bootstrap Integration

The Operations Manager already bootstraps Keycloak realms and clients during startup. Extend the bootstrap sequence to:

1. **Create the Forgejo OIDC client** in the `rig-platform` realm (via `forgejo.yaml` template)
2. **Retrieve the client secret** generated by Keycloak
3. **Store or pass the secret** to the Forgejo StatefulSet (via a Kubernetes Secret that Forgejo mounts)

This fits naturally into the existing `run_startup_tasks()` flow in `opi/core/startup.py`.

### 5. Database

Forgejo needs a PostgreSQL database. The ODCN cluster already has `rig-db` (CNPG). Create a `forgejo` database following the existing pattern:

```yaml
# In the bootstrap or via Operations Manager database provisioning
DATABASE_HOST: rig-db-rw.rig-prd-operations.svc.cluster.local
DATABASE_NAME: forgejo
DATABASE_USER: forgejo
DATABASE_SSL_MODE: require
```

Credentials stored in a SOPS-encrypted Secret, consistent with other services.

### 6. Ingress and Domain

Options for the ODCN domain:

| Option | URL | Notes |
|--------|-----|-------|
| A | `forgejo.rig.prd1.gn2.quattro.rijksapps.nl` | Follows existing pattern |
| B | `git.rijksapp.nl` | Shorter, user-friendly |
| C | `forgejo.rijksapp.nl` | Explicit service name |

TLS termination via HAProxy (existing ODCN pattern) or cert-manager. The ingress needs the same IP whitelist as other ODCN services (`147.181.0.0/16`).

### 7. Network Policies

Forgejo needs to communicate with:

| Target | Port | Purpose |
|--------|------|---------|
| `rig-db-rw` | 5432 | PostgreSQL database |
| `keycloak.rijksapp.nl` | 443 | OIDC token validation (outbound) |
| ArgoCD | - | Git clone (ArgoCD pulls from Forgejo) |
| Operations Manager | - | Git push (OPI pushes project configs) |
| Ingress controller | 3000 | HTTP traffic from users |

## Configuration Summary

### Environment Variables (StatefulSet)

| Variable | Value | Purpose |
|----------|-------|---------|
| `FORGEJO__server__ROOT_URL` | `https://forgejo.<domain>` | Public URL |
| `FORGEJO__server__DOMAIN` | `forgejo.<domain>` | Server domain |
| `FORGEJO__database__DB_TYPE` | `postgres` | Database type |
| `FORGEJO__database__HOST` | `rig-db-rw:5432` | Database host |
| `FORGEJO__database__SSL_MODE` | `require` | TLS to database |
| `FORGEJO__oauth2__ENABLED` | `true` | Enable OIDC |
| `FORGEJO__service__DISABLE_REGISTRATION` | `false` | Allow OIDC auto-registration |
| `FORGEJO__service__ALLOW_ONLY_EXTERNAL_REGISTRATION` | `true` | Force SSO (no local signup) |
| `FORGEJO__service__REQUIRE_SIGNIN_VIEW` | `true` | Private instance |
| `FORGEJO__security__INSTALL_LOCK` | `true` | Skip web installer |

### Secrets (SOPS-encrypted)

| Secret | Contents |
|--------|----------|
| `forgejo-db-credentials` | Database username, password |
| `forgejo-oidc-credentials` | Keycloak client secret for the `forgejo` client |
| `forgejo-admin-credentials` | Bootstrap admin (`rig-admin`) password and API token |

## Task Breakdown

1. **Create Keycloak YAML template** (`forgejo.yaml`) for the Forgejo OIDC client
2. **Create ODCN controller overlay** (StatefulSet, Ingress patches with OIDC env vars)
3. **Create ODCN config overlay** (bootstrap Job: admin user, OIDC auth source, repositories)
4. **Create Forgejo database** in CNPG (via Operations Manager or manual SQL)
5. **Create SOPS-encrypted secrets** (DB creds, OIDC client secret, admin password)
6. **Add bootstrap integration** in Operations Manager startup to provision the Keycloak client
7. **Add ArgoCD Application** for Forgejo in the ODCN cluster config
8. **Test end-to-end**: SSO login, repo creation, ArgoCD sync, OPI push

## Dependencies

- Keycloak with `rig-platform` realm (already deployed)
- CNPG `rig-db` cluster (already deployed)
- Ingress controller with TLS termination (already deployed)
- Operations Manager with Keycloak YAML template processing (already implemented)

## Risks and Considerations

- **OIDC client secret rotation**: If Keycloak rotates the client secret, Forgejo needs the updated secret. Consider storing it in a Kubernetes Secret that both systems read.
- **User mapping**: OIDC users get auto-created in Forgejo on first login. Decide whether to map SSO-Rijk identifiers to Forgejo usernames (email-based is simplest).
- **Admin access**: The bootstrap `rig-admin` account should remain as a local fallback in case OIDC is unavailable.
- **Git SSH access**: This document covers HTTPS only. SSH access would require additional service configuration (port 22 or a custom port) and is not needed for the initial deployment.
- **Repository migration**: If migrating repos from GitHub, Forgejo supports repository mirroring and import via API.
