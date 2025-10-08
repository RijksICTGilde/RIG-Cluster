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

4. **Configure in Keycloak UI**:
   - Add mapper: **Unrestricted XPath Attribute Importer**
   - XPath: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
   - User Attribute: `sso_rijk_collab_person_id`

5. **Override sub claim**: Add protocol mapper on OIDC client to map attribute to `sub` claim

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
