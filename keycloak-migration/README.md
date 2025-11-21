# Keycloak Migration Tools

This directory contains tools and documentation for Keycloak SSO-Rijk federation and transparent SSO migration.

## Problem

Extract SAML NameID from SSO-Rijk federation to enable transparent SSO through Keycloak intermediaries, ensuring consistent user identity during and after migration.

## Testing

**Before deploying to production, test locally first!**

See **[TESTING.md](./TESTING.md)** for complete testing guide.

Quick test with Docker:
```bash
task test-keycloak-custom-mapper-docker
```

Then check http://localhost:8080 → Create realm → Identity Providers → SAML → Mappers → Look for "Unrestricted XPath Attribute Importer"

## Quick Start

### For the Full Story

See **[INVESTIGATION-SUMMARY.md](./INVESTIGATION-SUMMARY.md)** for:
- Complete investigation of what we tried and why things didn't work
- Root cause analysis of Keycloak mapper limitations
- Technical deep-dive into SAML structure and Keycloak source code
- Step-by-step solution implementation

### Solution Overview

We created a **custom Keycloak mapper** that extracts SAML NameID to user attributes:

1. **Build the mapper**:
   ```bash
   task build-keycloak-custom-mapper
   ```

2. **Publish to GitHub** (optional):
   ```bash
   task publish-keycloak-custom-mapper
   ```

3. **Deploy to Keycloak**: Update deployment init container to download the JAR (see [custom-mapper/README.md](./custom-mapper/README.md))

4. **Configure in Keycloak UI** (see detailed configuration below)

## SSO Rijk Configuration

To enable transparent migration, we need to map the SSO Rijk user ID in both its original form and lowercase. This ensures our Keycloak provides the same attributes as SSO Rijk does directly.

### Required Identity Provider Mappers

Configure two mappers on the **sso-rijk** identity provider to import the NameID attribute twice:

1. **Mapper: sso-rijk-userid** (original value)
   - Type: **Unrestricted XPath Attribute Importer**
   - XPath Expression: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
   - User Attribute Name: `sso-rijk-userid`
   - Value Transformation: **AS_IS**
   - Sync Mode: **FORCE**

2. **Mapper: sso-rijk-userid-lowercase** (lowercase value)
   - Type: **Unrestricted XPath Attribute Importer**
   - XPath Expression: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
   - User Attribute Name: `sso-rijk-userid-lowercase`
   - Value Transformation: **LOWERCASE**
   - Sync Mode: **FORCE**

### Client Scope Configuration

In the **profile** client scope:

1. **Disable the default username mapper**
   - This prevents conflicts with our custom mappings

2. **Add custom protocol mappers**:

   **Mapper: preferred_username**
   - Mapper Type: **User Attribute**
   - User Attribute: `sso-rijk-userid-lowercase`
   - Token Claim Name: `preferred_username`
   - Claim JSON Type: **String**
   - Add to ID token: **ON**
   - Add to access token: **ON**
   - Add to userinfo: **ON**

   **Mapper: sub**
   - Mapper Type: **User Attribute**
   - User Attribute: `sso-rijk-userid`
   - Token Claim Name: `sub`
   - Claim JSON Type: **String**
   - Add to ID token: **ON**
   - Add to access token: **ON**
   - Add to userinfo: **ON**

### Why This Configuration?

This setup ensures that our Keycloak acts as a transparent intermediary:

- **sub** claim contains the original NameID value (e.g., `urn:collab:person:minbzk:nl:Uittenbroek`)
- **preferred_username** contains the lowercase version (e.g., `urn:collab:person:minbzk:nl:uittenbroek`)

This matches what SSO Rijk provides directly, making the migration transparent to client applications. When we remove the intermediary Keycloak, applications will receive the same claims.

## Files

- **[INVESTIGATION-SUMMARY.md](./INVESTIGATION-SUMMARY.md)** - Complete investigation summary
- **[nameid-mapper-investigation.md](./nameid-mapper-investigation.md)** - Detailed technical investigation notes
- **[backfill-sso-attributes.py](./backfill-sso-attributes.py)** - Temporary backfill script (superseded by custom mapper)
- **[custom-mapper/](./custom-mapper/)** - Custom Keycloak mapper implementation
  - Java source code
  - Maven build configuration
  - Deployment guide

## Why This Matters

**Transparent SSO** allows removing the Digilab Keycloak intermediary without user data migration:

```
Before:  SSO-Rijk → RIG Keycloak → Apps
During:  SSO-Rijk → Digilab → RIG Keycloak → Apps (same sub claim!)
After:   SSO-Rijk → RIG Keycloak → Apps (no migration needed!)
```

Without transparent SSO, removing Digilab would require recreating all users in RIG Keycloak.

## Built-in Mappers Don't Work

We tried:
- ❌ XPath Attribute Importer (restricted to AttributeStatement)
- ❌ User Session Note Mapper (doesn't contain NameID)
- ❌ Username Template Importer (can't set attributes, character validation issues)
- ❌ Standard Attribute Importer (attribute not present)

See [INVESTIGATION-SUMMARY.md](./INVESTIGATION-SUMMARY.md) for full details on why each approach failed.

## Support

For questions or issues:
1. Check the investigation summary
2. Review custom mapper README
3. Check Keycloak logs with debug logging enabled
4. Contact the platform team
