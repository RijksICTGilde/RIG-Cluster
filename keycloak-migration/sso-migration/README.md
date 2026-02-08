# SSO-Rijk IDP Migration Guide

This guide covers migrating the `sso-rijk` IDP from OIDC (via Digilab) to SAML (direct SSO-Rijk connection).

## Overview

The migration swaps the active IDP without disrupting users:
- **Before**: `sso-rijk` (OIDC via Digilab) is active, `sso-rijk-direct` (SAML) exists but is disabled
- **After**: `sso-rijk` (SAML, former `sso-rijk-direct`) is active, `sso-rijk-obsolete` (former OIDC) is disabled

The swap preserves user federated identities because they reference the IDP by **alias** (`sso-rijk`), which is taken over by the SAML IDP.

## Prerequisites

1. **Bootstrap has run**: Both IDPs exist in Keycloak (OIDC active, SAML disabled)
2. **SSO-Rijk registration**: Register with SSO-Rijk using:
   - Entity ID: `https://keycloak.rijksapp.nl/realms/rig-platform`
   - ACS URL: `https://keycloak.rijksapp.nl/realms/rig-platform/broker/sso-rijk/endpoint`

---

## Quick Migration (with Bootstrap Integration)

This is the recommended approach when using the operations-manager bootstrap.

### Step 1: Dry Run

```bash
# Port-forward to database first
kubectl port-forward svc/rig-db-rw -n rig-system 5432:5432

# Get credentials
KEYCLOAK_PWD=$(kubectl get secret -n rig-system keycloak-credentials -o jsonpath='{.data.admin-password}' | base64 -d)
DB_PWD=$(kubectl get secret -n rig-system keycloak-db-credentials -o jsonpath='{.data.password}' | base64 -d)

# Dry run - shows what will happen
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password "$KEYCLOAK_PWD" \
  --old-alias sso-rijk \
  --new-alias sso-rijk-direct \
  --db-password "$DB_PWD" \
  --bootstrap-yaml /path/to/operations-manager/python/opi/configs/keycloak/bootstrap.yaml \
  --dry-run
```

### Step 2: Execute Migration

```bash
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password "$KEYCLOAK_PWD" \
  --old-alias sso-rijk \
  --new-alias sso-rijk-direct \
  --db-password "$DB_PWD" \
  --bootstrap-yaml /path/to/operations-manager/python/opi/configs/keycloak/bootstrap.yaml
```

This will:
1. Rename `sso-rijk` (OIDC) to `sso-rijk-obsolete` (disabled)
2. Rename `sso-rijk-direct` (SAML) to `sso-rijk` (enabled)
3. Update `bootstrap.yaml` to match the new state

### Step 3: Commit and Restart

```bash
# Commit the updated bootstrap.yaml
cd /path/to/operations-manager
git add python/opi/configs/keycloak/bootstrap.yaml
git commit -m "Update bootstrap.yaml after SSO-Rijk migration"

# Restart Keycloak to clear caches
kubectl rollout restart deployment/keycloak -n rig-system
```

### Step 4: Verify

1. Test login with an existing user
2. Verify user attributes are populated correctly
3. Check no new user accounts were created (same user linked)

### Rollback (if needed)

The old IDP is preserved as `sso-rijk-obsolete`. To rollback:
1. Restore database from backup
2. Restore bootstrap.yaml from git or backup
3. Restart Keycloak

---

## Table of Contents

1. [Local Testing (Kind Cluster)](#local-testing-kind-cluster)
2. [ODCN Production Deployment](#odcn-production-deployment)
   - [Route A: Automatic via Script](#route-a-automatic-via-script)
   - [Route B: Manual via Keycloak UI](#route-b-manual-via-keycloak-ui)
3. [Adding keycloak.rijksapp.nl (Dual URL)](#adding-keycloakrijksappnl-dual-url)

---

## Local Testing (Kind Cluster)

Test the migration script locally using OIDC (pointing to digilab) before deploying to production.

### Prerequisites

```bash
# Install Python dependencies
pip install requests psycopg2-binary
```

### Step 1: Set Up Database Access

```bash
# Terminal 1: Port-forward to the database
kubectl port-forward svc/rig-db-rw -n rig-system 5432:5432
```

### Step 2: Get Credentials

```bash
# Get Keycloak admin password
kubectl get secret -n rig-system keycloak-credentials -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Get database password
kubectl get secret -n rig-system keycloak-db-credentials -o jsonpath='{.data.password}' | base64 -d; echo
```

### Step 3: Check Current Status

```bash
python migrate_sso_idp.py status \
  --keycloak-url https://keycloak.kind \
  --admin-password <KEYCLOAK_PASSWORD> \
  --insecure
```

### Step 4: Add New OIDC IDP

For local testing, create an OIDC IDP pointing to digilab:

```bash
python migrate_sso_idp.py add-idp \
  --keycloak-url https://keycloak.kind \
  --admin-password <KEYCLOAK_PASSWORD> \
  --type oidc \
  --alias sso-rijk-new \
  --discovery-url https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration \
  --client-id opi \
  --client-secret IxcHjClinvaTw8CsrQ7PUmloO2JohxaP \
  --insecure
```

### Step 5: Add Mappers

```bash
python migrate_sso_idp.py add-mappers \
  --keycloak-url https://keycloak.kind \
  --admin-password <KEYCLOAK_PASSWORD> \
  --idp-alias sso-rijk-new \
  --insecure
```

### Step 6: Test Login (Optional)

Before swapping, you can test the new IDP:
1. Go to Keycloak Admin UI → Identity Providers → `sso-rijk-new`
2. Copy the redirect URI
3. Test login flow manually

### Step 7: Database Swap (Dry Run)

```bash
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.kind \
  --admin-password <KEYCLOAK_PASSWORD> \
  --old-alias sso-rijk \
  --new-alias sso-rijk-new \
  --db-host localhost \
  --db-port 5432 \
  --db-password <DB_PASSWORD> \
  --dry-run \
  --insecure
```

### Step 8: Execute Swap

```bash
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.kind \
  --admin-password <KEYCLOAK_PASSWORD> \
  --old-alias sso-rijk \
  --new-alias sso-rijk-new \
  --db-host localhost \
  --db-port 5432 \
  --db-password <DB_PASSWORD> \
  --insecure
```

### Step 9: Restart Keycloak

```bash
kubectl rollout restart deployment/keycloak -n rig-system
```

### Step 10: Verify

1. Log in via SSO-Rijk
2. Check user attributes are populated correctly
3. Verify existing user links to same account (no duplicate created)

---

## ODCN Production Deployment

### Prerequisites

1. **SSO-Rijk Registration**: Quattro must be registered as a SAML SP with SSO-Rijk
   - Entity ID: `https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform`
   - ACS URL: `https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform/broker/sso-rijk-new/endpoint`

2. **Custom Mapper JAR**: Verify `keycloak-saml-nameid-mapper-1.0.0.jar` is deployed

3. **Database Backup**: Always backup before making changes

### SSO-Rijk SAML Endpoints

| Endpoint | URL |
|----------|-----|
| Entity ID / Metadata | `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/metadata` |
| SSO URL | `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/single-sign-on` |
| Logout URL | `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/logout` |

---

### Route A: Automatic via Script

#### Step 1: Set Up Database Access

```bash
# Terminal 1: Port-forward to database
kubectl port-forward svc/rig-db-rw -n rig-system 5432:5432
```

#### Step 2: Get Credentials

```bash
# Keycloak admin password
kubectl get secret -n rig-system keycloak-credentials -o jsonpath='{.data.admin-password}' | base64 -d; echo

# Database password
kubectl get secret -n rig-system keycloak-db-credentials -o jsonpath='{.data.password}' | base64 -d; echo
```

#### Step 3: Check Current Status

```bash
python migrate_sso_idp.py status \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <KEYCLOAK_PASSWORD>
```

#### Step 4: Add New SAML IDP

```bash
python migrate_sso_idp.py add-idp \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <KEYCLOAK_PASSWORD> \
  --type saml \
  --alias sso-rijk-new \
  --display-name "SSO Rijk (Direct)" \
  --entity-id https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/metadata \
  --sso-url https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/single-sign-on \
  --logout-url https://engine.spbroker-prd.bzk.sson.overheid-i.nl/logout
```

#### Step 5: Add SAML Mappers

```bash
python migrate_sso_idp.py add-mappers \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <KEYCLOAK_PASSWORD> \
  --idp-alias sso-rijk-new
```

#### Step 6: Test Login with New IDP

Before swapping, test the new IDP works:
1. Access: `https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform/broker/sso-rijk-new/endpoint`
2. Verify SSO-Rijk login flow works
3. Check user attributes are mapped correctly

#### Step 7: Swap IDPs (Dry Run First)

```bash
# Dry run
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <KEYCLOAK_PASSWORD> \
  --old-alias sso-rijk \
  --new-alias sso-rijk-new \
  --db-host localhost \
  --db-port 5432 \
  --db-password <DB_PASSWORD> \
  --dry-run

# Execute (after verifying dry-run output)
python migrate_sso_idp.py swap \
  --keycloak-url https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl \
  --admin-password <KEYCLOAK_PASSWORD> \
  --old-alias sso-rijk \
  --new-alias sso-rijk-new \
  --db-host localhost \
  --db-port 5432 \
  --db-password <DB_PASSWORD>
```

#### Step 8: Update SSO-Rijk Registration

Contact SSO-Rijk administrators to update the ACS URL:
- Old: `.../broker/sso-rijk-new/endpoint`
- New: `.../broker/sso-rijk/endpoint`

#### Step 9: Restart Keycloak

```bash
kubectl rollout restart deployment/keycloak -n rig-system
```

---

### Route B: Manual via Keycloak UI

Use this if you prefer to configure via the Keycloak Admin Console.

#### Step 1: Backup Database

```bash
# Port-forward and backup
kubectl port-forward svc/rig-db-rw -n rig-system 5432:5432

pg_dump -h localhost -U keycloak keycloak > keycloak_backup_$(date +%Y%m%d_%H%M%S).sql
```

#### Step 2: Create New SAML IDP

1. Go to **Keycloak Admin Console** → `rig-platform` realm
2. Navigate to **Identity Providers** → **Add provider** → **SAML v2.0**
3. Configure:

| Field | Value |
|-------|-------|
| Alias | `sso-rijk-new` |
| Display Name | `SSO Rijk (Direct)` |
| Enabled | `ON` |
| Trust Email | `ON` |
| Sync Mode | `Force` |
| Single Sign-On Service URL | `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/single-sign-on` |
| Single Logout Service URL | `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/logout` |
| NameID Policy Format | `Persistent` |
| Principal Type | `Subject NameID` |
| Entity ID (Service Provider) | `https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform` |

4. Click **Save**

#### Step 3: Import from SAML Metadata (Alternative)

Instead of manual configuration, you can import from metadata:

1. In the IDP creation screen, find **Import from URL**
2. Enter: `https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/metadata`
3. Click **Import**
4. Adjust settings as needed and save

#### Step 4: Add SAML Mappers

Navigate to the new IDP → **Mappers** tab → **Add mapper**

**Mapper 1: sso-rijk-userid**
| Field | Value |
|-------|-------|
| Name | `sso-rijk-userid` |
| Mapper Type | `Unrestricted XPath Attribute Importer` |
| XPath Expression | `//*[local-name()='Subject']/*[local-name()='NameID']/text()` |
| User Attribute | `sso-rijk-userid` |
| Sync Mode | `Force` |

**Mapper 2: sso-rijk-userid-lowercase**
| Field | Value |
|-------|-------|
| Name | `sso-rijk-userid-lowercase` |
| Mapper Type | `Unrestricted XPath Attribute Importer` |
| XPath Expression | `//*[local-name()='Subject']/*[local-name()='NameID']/text()` |
| User Attribute | `sso-rijk-userid-lowercase` |
| Value Transformation | `LOWERCASE` |
| Sync Mode | `Inherit` |

**Mapper 3: email**
| Field | Value |
|-------|-------|
| Name | `email` |
| Mapper Type | `Attribute Importer` |
| Attribute Name | `urn:rijksoverheid:federation:emailAddress` |
| User Attribute | `email` |
| Sync Mode | `Inherit` |

**Mapper 4: firstName**
| Field | Value |
|-------|-------|
| Name | `firstName` |
| Mapper Type | `Attribute Importer` |
| Attribute Name | `urn:rijksoverheid:federation:givenName` |
| User Attribute | `firstName` |
| Sync Mode | `Inherit` |

**Mapper 5: lastName**
| Field | Value |
|-------|-------|
| Name | `lastName` |
| Mapper Type | `Attribute Importer` |
| Attribute Name | `urn:rijksoverheid:federation:surName` |
| User Attribute | `lastName` |
| Sync Mode | `Inherit` |

**Mapper 6: organizationName**
| Field | Value |
|-------|-------|
| Name | `organizationName` |
| Mapper Type | `Attribute Importer` |
| Attribute Name | `urn:rijksoverheid:federation:organizationDisplayName` |
| User Attribute | `organization.name` |
| Sync Mode | `Inherit` |

**Mapper 7: organizationNumber**
| Field | Value |
|-------|-------|
| Name | `organizationNumber` |
| Mapper Type | `Attribute Importer` |
| Attribute Name | `urn:rijksoverheid:federation:organizationNumber` |
| User Attribute | `organization.number` |
| Sync Mode | `Inherit` |

#### Step 5: Test New IDP

1. Open incognito browser
2. Go to a test application configured to use `sso-rijk-new`
3. Verify login redirects to SSO-Rijk and attributes are mapped

#### Step 6: Database Swap via SQL

Connect to the database and execute:

```sql
-- Get realm ID
SELECT id FROM realm WHERE name = 'rig-platform';
-- Note the realm_id for use below

-- Check current state
SELECT internal_id, provider_alias, provider_id
FROM identity_provider
WHERE realm_id = '<realm_id>' AND provider_alias IN ('sso-rijk', 'sso-rijk-new');

-- Check federated identities that will be preserved
SELECT COUNT(*) FROM federated_identity
WHERE realm_id = '<realm_id>' AND identity_provider = 'sso-rijk';

-- Get internal IDs
-- old_internal_id = internal_id where provider_alias = 'sso-rijk'
-- new_internal_id = internal_id where provider_alias = 'sso-rijk-new'

-- Step 1: Delete old IDP mappers
DELETE FROM identity_provider_mapper
WHERE identity_provider_id = '<old_internal_id>';

-- Step 2: Delete old IDP config
DELETE FROM identity_provider_config
WHERE identity_provider_id = '<old_internal_id>';

-- Step 3: Delete old IDP
DELETE FROM identity_provider
WHERE internal_id = '<old_internal_id>';

-- Step 4: Rename new IDP to old alias
UPDATE identity_provider
SET provider_alias = 'sso-rijk', display_name = 'SSO Rijk'
WHERE internal_id = '<new_internal_id>';

-- Step 5: Update mapper references
UPDATE identity_provider_mapper
SET idp_alias = 'sso-rijk'
WHERE idp_alias = 'sso-rijk-new';
```

#### Step 7: Update SSO-Rijk Registration

Contact SSO-Rijk administrators to update the ACS URL to use `sso-rijk` alias.

#### Step 8: Restart Keycloak

```bash
kubectl rollout restart deployment/keycloak -n rig-system
```

---

## Verification Checklist

After migration, verify:

- [ ] Existing users can log in via SSO-Rijk
- [ ] No new user accounts created (links to existing account)
- [ ] User attribute `sso-rijk-userid` = `urn:collab:person:...`
- [ ] Token claim `sub` = SSO-Rijk NameID
- [ ] Token claim `preferred_username` = lowercase NameID
- [ ] Organization claims present in tokens
- [ ] Existing projects/realms work without changes

## Rollback

If issues occur after the swap:

```bash
# Restore from backup
psql -h localhost -U keycloak keycloak < keycloak_backup_YYYYMMDD_HHMMSS.sql

# Restart Keycloak
kubectl rollout restart deployment/keycloak -n rig-system
```

## Troubleshooting

### User gets "Account already exists" error

The federated identity didn't match. Check:
```sql
SELECT federated_user_id FROM federated_identity
WHERE realm_id = '<realm_id>' AND identity_provider = 'sso-rijk';
```
Compare with the NameID from SSO-Rijk SAML response.

### Mappers not showing in UI

The custom XPath mapper JAR may not be loaded. Check Keycloak logs:
```bash
kubectl logs -n rig-system deployment/keycloak | grep -i mapper
```

### SAML signature validation fails

Temporarily disable signature validation in IDP config, or import the correct signing certificate from SSO-Rijk metadata.

---

## Adding keycloak.rijksapp.nl (Dual URL)

This section covers adding a second URL (`keycloak.rijksapp.nl`) to Keycloak using Let's Encrypt certificates. This enables a gradual transition from the quattro domain to the new rijksapp.nl domain.

### How It Works

Keycloak can serve multiple URLs, but has a **canonical hostname** (`KC_HOSTNAME`) used in:
- OIDC token `issuer` claim
- SAML Entity ID
- Redirect URIs in metadata

**Transition Strategy (Option 3):**
1. Add second URL now (keycloak.rijksapp.nl)
2. Both URLs work, but tokens reference the original hostname
3. Later, update `KC_HOSTNAME` to make rijksapp.nl the canonical URL
4. Eventually, remove the quattro hostname

### Prerequisites

1. **DNS Configuration**: Point `keycloak.rijksapp.nl` to the cluster ingress IP
2. **Port 80 Access**: Let's Encrypt HTTP-01 challenge requires public port 80 access

### Files Created

The following files have been added to the ODCN overlay:

```
infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn/
├── kustomization.yaml          # Updated to include new resources
├── issuer-letsencrypt.yaml     # Let's Encrypt Issuer for cert-manager
└── ingress-rijksapp.yaml       # Second Ingress for keycloak.rijksapp.nl
```

### Step 1: Verify DNS

Ensure DNS is configured before deploying:

```bash
# Check DNS resolution
dig keycloak.rijksapp.nl

# Should return the cluster ingress IP
```

### Step 2: Deploy the Changes

The manifests are already in the ODCN overlay. Deploy via ArgoCD sync or manually:

```bash
# Preview what will be created
kustomize build infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn

# Apply manually (if not using ArgoCD)
kubectl apply -k infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn
```

### Step 3: Verify Certificate Issuance

```bash
# Check Issuer status
kubectl describe issuer letsencrypt-keycloak -n rig-system

# Check Certificate status (created automatically by cert-manager)
kubectl get certificates -n rig-system

# Check for ACME challenges (if certificate is pending)
kubectl get challenges -n rig-system
```

### Step 4: Verify Both URLs Work

```bash
# Test quattro URL (existing)
curl -I https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/health

# Test rijksapp URL (new)
curl -I https://keycloak.rijksapp.nl/health
```

### Step 5: Update SSO-Rijk Registration (When Ready)

When transitioning to the new URL as canonical:

1. Register new SAML SP with SSO-Rijk:
   - Entity ID: `https://keycloak.rijksapp.nl/realms/rig-platform`
   - ACS URL: `https://keycloak.rijksapp.nl/realms/rig-platform/broker/sso-rijk/endpoint`

2. Update `KC_HOSTNAME` in the deployment patch:
   ```yaml
   - op: replace
     path: /spec/template/spec/containers/0/env/6/value
     value: "https://keycloak.rijksapp.nl"
   ```

3. Restart Keycloak:
   ```bash
   kubectl rollout restart deployment/keycloak -n rig-system
   ```

### Generated Resources

**Issuer (issuer-letsencrypt.yaml):**
```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: letsencrypt-keycloak
  namespace: rig-system
spec:
  acme:
    email: rig-platform@rijksoverheid.nl
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-keycloak-key
    solvers:
    - http01:
        ingress:
          serviceType: ClusterIP
          ingressTemplate:
            metadata:
              annotations:
                haproxy.router.openshift.io/ip_whitelist: "0.0.0.0/0"
```

**Ingress (ingress-rijksapp.yaml):**
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: keycloak-rijksapp
  namespace: rig-system
  annotations:
    cert-manager.io/issuer: letsencrypt-keycloak
    haproxy.router.openshift.io/ip_whitelist: "0.0.0.0/0"
spec:
  rules:
    - host: keycloak.rijksapp.nl
      http:
        paths:
          - backend:
              service:
                name: keycloak
                port:
                  number: 8080
            path: /
            pathType: Prefix
  tls:
    - hosts:
        - keycloak.rijksapp.nl
      secretName: keycloak-rijksapp-tls
```

### Troubleshooting

#### Certificate not issued

```bash
# Check Issuer status
kubectl describe issuer letsencrypt-keycloak -n rig-system

# Check Certificate events
kubectl describe certificate keycloak-rijksapp-tls -n rig-system

# Check challenges
kubectl get challenges -n rig-system
kubectl describe challenge <challenge-name> -n rig-system
```

#### HTTP-01 challenge failing

- Verify DNS points to cluster ingress
- Ensure port 80 is accessible from the internet
- Check the `/.well-known/acme-challenge/` path is reachable
- Verify HAProxy/OpenShift router allows the challenge traffic

#### Token issuer mismatch

If clients receive tokens with the wrong issuer:
- This is expected during transition - tokens use `KC_HOSTNAME`
- Clients should accept both issuers during transition
- Update `KC_HOSTNAME` when ready to make rijksapp.nl canonical

### Full Transition Checklist

When ready to make `keycloak.rijksapp.nl` the canonical URL:

- [ ] DNS configured and verified
- [ ] Let's Encrypt certificate issued and valid
- [ ] Test login via new URL works
- [ ] SSO-Rijk registration updated for new URL
- [ ] All clients updated to accept new issuer
- [ ] Update `KC_HOSTNAME` to `https://keycloak.rijksapp.nl`
- [ ] Restart Keycloak
- [ ] Verify tokens have correct issuer
- [ ] (Later) Remove old quattro ingress
