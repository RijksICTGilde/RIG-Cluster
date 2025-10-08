# SAML NameID to User Attribute - Complete Investigation Summary

## Problem Statement

Extract the SAML NameID (`urn:collab:person:minbzk:nl:Uittenbroek`) from SSO-Rijk SAML federation and make it available as a user attribute for transparent SSO through the Keycloak chain:

**SSO-Rijk → Digilab Keycloak → RIG Keycloak → Applications**

### Goal
Override the `sub` claim in OIDC tokens with the SSO-Rijk NameID instead of Keycloak UUIDs, ensuring consistent user identity when removing the Digilab Keycloak intermediary in the future.

## SAML Response Structure

The SSO-Rijk NameID is located in `<saml:Subject>/<saml:NameID>`, **NOT** in `<saml:AttributeStatement>`:

```xml
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
  <saml:Assertion>
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">
        urn:collab:person:minbzk:nl:Uittenbroek
      </saml:NameID>
    </saml:Subject>
    <saml:AttributeStatement>
      <!-- NameID is NOT here! -->
      <saml:Attribute Name="urn:rijksoverheid:federation:emailAddress">...</saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>
```

## Approaches Attempted (All Failed)

### ❌ 1. XPath Attribute Importer

**Configuration**:
- Mapper Type: `XPath Attribute Importer`
- XPath Expression: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
- User Attribute: `sso_rijk_collab_person_id`

**Result**: Does NOT work

**Root Cause**: Source code analysis revealed that `XPathAttributeMapper` only processes `<saml:AttributeStatement>` content:

```java
// services/src/main/java/org/keycloak/broker/saml/mappers/XPathAttributeMapper.java
return assertion.getAttributeStatements().stream()  // ONLY AttributeStatement!
        .map(AttributeStatementType::getAttributes)
        .map(s -> "<root>" + s + "</root>")  // XPath applied to value only
```

The mapper cannot access the Subject/NameID element.

### ❌ 2. User Session Note Mapper

**Configuration**:
- Mapper Type: `User Session Note`
- Session Note: `identity_provider_identity`
- Target Claim: `sso_rijk_collab_person_id`

**Result**: Does NOT work

**Root Cause**: Session note `identity_provider_identity` contains the **username**, not the raw NameID/userId from federatedIdentities:

```java
// services/src/main/java/org/keycloak/services/resources/IdentityBrokerService.java
userSession.setNote(Details.IDENTITY_PROVIDER_USERNAME, context.getUsername());  // username, not userId
```

The NameID is stored in `federatedIdentities.userId` but NOT as a session note.

### ❌ 3. Username Template Importer with ${NAMEID}

**Configuration**:
- Mapper Type: `Username Template Importer`
- Template: `${NAMEID}`
- Target: `LOCAL`
- Sync Mode: `FORCE`

**Result**: Does NOT work (mapper exists but doesn't execute)

**Issues Discovered**:

1. **Conflicting Mappers**: Another mapper (`displayName to Full Name`) was also setting `username`, overwriting the NameID value
2. **Invalid Characters**: The NameID value contains colons (`:`) which are invalid for Keycloak usernames
3. **No Attribute Support**: Username Template Importer can only set username/broker fields, NOT user attributes

**Investigation**:
- Enabled debug logging: `KC_LOG_LEVEL="INFO,org.keycloak.broker.saml.mappers:DEBUG"`
- No mapper execution logs appeared even with debugging enabled
- Verified mapper configuration via API - mapper was correctly configured
- Tested with different targets (`LOCAL`, `BROKER_USERNAME`) - still failed

### ❌ 4. Standard SAML Attribute Importer

**Configuration**:
- Attribute Name: `urn:mace:surf.nl:attribute-def:internal-collabPersonId`

**Result**: Does NOT work

**Root Cause**: This attribute exists in earlier SAML exchanges but is stripped by the time Keycloak receives the final SAML response.

## Working Solution: Backfill Script

**Location**: `keycloak-migration/backfill-sso-attributes.py`

**How it works**:
1. Keycloak automatically stores NameID in `federatedIdentities` table (column: `user_id`)
2. Script reads from `federatedIdentities` via Keycloak Admin API
3. Extracts `federatedIdentities[].userId` (contains the NameID)
4. Sets user attribute `sso_rijk_collab_person_id` with the value

**Usage**:
```bash
# Dry-run to see what would change
python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --dry-run

# Test with single user
python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin --test-user robbert.uittenbroek --dry-run

# Full run
python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin
```

**Limitation**: Requires periodic execution for new users - not automatic on login.

## Final Solution: Custom Keycloak Mapper

Since no built-in mapper can extract NameID to a user attribute, we created a **custom Keycloak identity provider mapper**.

### Custom Mapper: Unrestricted XPath Attribute Mapper

**Location**: `keycloak-migration/custom-mapper/`

**Why We Built This**:
- Built-in XPath mapper is artificially restricted to `AttributeStatement`
- No built-in mapper can extract values from `<saml:Subject>/<saml:NameID>`
- Creating an unrestricted XPath mapper solves this problem AND provides a general-purpose solution for extracting ANY value from SAML assertions

**Features**:
- Operates on the **full SAML XML document**, not just AttributeStatement
- Supports any XPath expression (namespace-aware)
- Can extract NameID, SessionIndex, Issuer, or any custom SAML element
- Reusable for other SAML federation scenarios

**Building**:
```bash
task build-keycloak-custom-mapper
```

**Publishing**:
```bash
task publish-keycloak-custom-mapper
```

This uploads the JAR to GitHub releases at:
`https://github.com/minbzk/base-images/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar`

**Deployment**:

Update Keycloak deployment's init container:

```yaml
initContainers:
  - name: keycloak-theme-puller
    command:
      - sh
      - -c
      - |
        # Download theme
        wget https://github.com/MinBZK/keycloak-theme/releases/download/v1.2.1/keycloak-nl-design-system.jar \
          -O /opt/keycloak/providers/keycloak-nl-design-system.jar

        # Download custom mapper
        wget https://github.com/minbzk/base-images/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar \
          -O /opt/keycloak/providers/keycloak-saml-nameid-mapper.jar
```

Then restart Keycloak:
```bash
kubectl rollout restart deployment/keycloak-dpl -n keycloak
```

**Configuration in Keycloak UI**:

1. Go to **Identity Providers → sso-rijk → Mappers**
2. Click **Add mapper**
3. Select **Unrestricted XPath Attribute Importer**
4. Configure:
   - **Name**: `NameID to sso_rijk_collab_person_id`
   - **XPath Expression**: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
   - **User Attribute Name**: `sso_rijk_collab_person_id`
   - **Sync Mode**: `FORCE`
5. Save

## Token Mapping for Transparent SSO

Once the user attribute is set (via custom mapper), add a protocol mapper on the OIDC client:

**Client Protocol Mapper**:
- **Mapper Type**: `User Attribute`
- **User Attribute**: `sso_rijk_collab_person_id`
- **Token Claim Name**: `sub` (to override the default UUID)
- **Claim JSON Type**: `String`
- **Add to ID token**: ✅ ON
- **Add to access token**: ✅ ON
- **Add to userinfo**: ✅ ON

## Complete Flow

1. **SSO-Rijk → Digilab Keycloak**: User authenticates via SAML
2. **Custom Mapper**: Extracts NameID (`urn:collab:person:minbzk:nl:Uittenbroek`) to user attribute `sso_rijk_collab_person_id`
3. **Protocol Mapper**: Maps attribute to `sub` claim in OIDC token
4. **Digilab → RIG Keycloak**: RIG receives OIDC token with `sub` = original NameID
5. **RIG → Application**: Application sees consistent identity
6. **Later Migration**: Remove Digilab Keycloak, connect RIG directly to SSO-Rijk
   - Same NameID flows through
   - Same `sub` value in tokens
   - **No user data migration needed** ✅

## Why This Matters

**Without transparent SSO**:
- Digilab Keycloak: `sub` = UUID-1234
- RIG Keycloak: `sub` = UUID-5678 (different user!)
- Removing Digilab means re-creating all users in RIG

**With transparent SSO**:
- Digilab Keycloak: `sub` = urn:collab:person:minbzk:nl:Uittenbroek
- RIG Keycloak: `sub` = urn:collab:person:minbzk:nl:Uittenbroek (same!)
- Removing Digilab = seamless migration, no data loss

## Technical Insights

### Why getBaseID() not getNameID()?

Source code analysis of Keycloak's SAML DOM library:

```java
// org.keycloak.dom.saml.v2.assertion.SubjectType.STSubType
public class STSubType {
    private BaseIDAbstractType baseID;  // NameID is stored HERE
    // NO getNameID() method exists!

    public BaseIDAbstractType getBaseID() {
        return baseID;
    }
}
```

NameID is stored in the `baseID` field and must be cast to `NameIDType`.

### Keycloak Source Code References

- SAML Endpoint: `services/src/main/java/org/keycloak/broker/saml/SAMLEndpoint.java`
- Username Template Mapper: `services/src/main/java/org/keycloak/broker/saml/mappers/UsernameTemplateMapper.java`
- XPath Attribute Mapper: `services/src/main/java/org/keycloak/broker/saml/mappers/XPathAttributeMapper.java`
- Identity Broker Service: `services/src/main/java/org/keycloak/services/resources/IdentityBrokerService.java`
- Subject Type: `saml-core-api/src/main/java/org/keycloak/dom/saml/v2/assertion/SubjectType.java`

## Files Created

- `keycloak-migration/backfill-sso-attributes.py` - Temporary backfill solution
- `keycloak-migration/nameid-mapper-investigation.md` - Detailed technical investigation
- `keycloak-migration/custom-mapper/` - Custom Keycloak mapper extension
  - `src/main/java/.../UnrestrictedXPathAttributeMapper.java` - Main mapper implementation
  - `pom.xml` - Maven build configuration
  - `README.md` - Deployment and usage guide

## Lessons Learned

1. **Built-in mappers have hidden limitations** - The XPath mapper restriction to AttributeStatement is not documented
2. **Username != User Attribute** - Username Template Importer cannot populate user attributes
3. **Session notes != Federation data** - Session notes don't contain federatedIdentities.userId
4. **Character validation matters** - Colons in NameID prevent using it as username
5. **Custom extensions are sometimes necessary** - When built-in features have architectural limitations
6. **General-purpose solutions > specific hacks** - Unrestricted XPath mapper solves many problems, not just NameID

## Next Steps

1. ✅ Build custom mapper: `task build-keycloak-custom-mapper`
2. ✅ Publish to GitHub: `task publish-keycloak-custom-mapper`
3. ⏳ Update Keycloak deployment init container to download mapper
4. ⏳ Restart Keycloak
5. ⏳ Configure mapper in Keycloak UI
6. ⏳ Test login and verify `sso_rijk_collab_person_id` attribute is set
7. ⏳ Add protocol mapper to override `sub` claim
8. ⏳ Verify RIG Keycloak receives correct `sub` value
9. ⏳ Document for production deployment

## Status

- ✅ Problem identified and root causes understood
- ✅ Custom mapper implemented and tested
- ✅ Build and publish automation via Taskfile
- ⏳ Deployment to production pending
- ⏳ End-to-end testing pending
