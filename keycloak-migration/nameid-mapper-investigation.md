# SAML NameID to User Attribute Mapper Investigation

## Problem Statement

Need to extract the SAML NameID (`urn:collab:person:minbzk.nl:Uittenbroek`) from SSO-Rijk federation and make it available in tokens for transparent SSO through the Keycloak chain: SSO-Rijk → Digilab Keycloak → RIG Keycloak → Applications.

**Goal**: Override the `sub` claim with the SSO-Rijk NameID instead of Keycloak UUIDs.

## SAML Response Structure

The SSO-Rijk SAML response contains the identity in the NameID field:

```xml
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
                xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
    <saml:Assertion>
        <saml:Subject>
            <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">
                urn:collab:person:minbzk:nl:Uittenbroek
            </saml:NameID>
        </saml:Subject>
        <saml:AttributeStatement>
            <!-- NameID is NOT in AttributeStatement -->
            <saml:Attribute Name="urn:rijksoverheid:federation:emailAddress">...</saml:Attribute>
            <saml:Attribute Name="urn:rijksoverheid:federation:givenName">...</saml:Attribute>
            <!-- ... other attributes ... -->
        </saml:AttributeStatement>
    </saml:Assertion>
</samlp:Response>
```

**Key Point**: The NameID is in `<saml:Subject>`, NOT in `<saml:AttributeStatement>`.

## Approaches Investigated

### 1. ❌ XPath Attribute Importer

**Tested**: XPath Attribute Importer mapper
**XPath Expression**: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
**Result**: Does NOT work

**Reason**: Analyzed source code at `services/src/main/java/org/keycloak/broker/saml/mappers/XPathAttributeMapper.java`:

```java
private List<String> findAttributeValuesInContext(String attributeName, String attributeXPath, BrokeredIdentityContext context) {
    AssertionType assertion = (AssertionType) context.getContextData().get(SAMLEndpoint.SAML_ASSERTION);

    return assertion.getAttributeStatements().stream()  // <-- ONLY AttributeStatement!
            .map(AttributeStatementType::getAttributes)
            .flatMap(Collection::stream)
            .filter(elementWith(attributeName))
            .map(AttributeStatementType.ASTChoiceType::getAttribute)
            .map(AttributeType::getAttributeValue)  // <-- ONLY AttributeValue!
            .flatMap(Collection::stream)
            .filter(String.class::isInstance)
            .map(Object::toString)
            .map(s -> "<root>" + s + "</root>")  // <-- XPath applied to value only
            .map(applyXPath(attributeXPath))
```

**Conclusion**: XPathAttributeMapper only processes `<saml:AttributeStatement>/<saml:Attribute>/<saml:AttributeValue>` content, NOT the Subject/NameID.

### 2. ❌ User Session Note Mapper

**Tested**: User Session Note protocol mapper with `identity_provider_identity`
**Result**: Does NOT work

**Reason**: Session note `identity_provider_identity` contains the **username**, not the raw NameID/userId from federatedIdentities.

Source: `services/src/main/java/org/keycloak/services/resources/IdentityBrokerService.java` (line ~145):

```java
if (userSession != null && userSession.getNote(Details.IDENTITY_PROVIDER) == null) {
    userSession.setNote(Details.IDENTITY_PROVIDER, context.getIdpConfig().getAlias());
    userSession.setNote(Details.IDENTITY_PROVIDER_USERNAME, context.getUsername());  // <-- username, not userId
}
```

The NameID is stored in `federatedIdentities.userId` but NOT as a session note.

### 3. ❌ Username Template Importer with ${NAMEID}

**Tested**: Username Template Importer mapper
**Configuration**:
- Mapper Type: `Username Template Importer`
- Template: `${NAMEID}`
- Target: `LOCAL`
- Sync Mode: `FORCE`

**Result**: Does NOT extract NameID value (username remains empty/unset)

**Source Code Analysis**: `services/src/main/java/org/keycloak/broker/saml/mappers/UsernameTemplateMapper.java` (line 167-171):

```java
} else if (variable.equals("NAMEID")) {
    SubjectType subject = assertion.getSubject();
    SubjectType.STSubType subType = subject.getSubType();
    NameIDType subjectNameID = (NameIDType) subType.getBaseID();  // <-- Uses getBaseID()!
    m.appendReplacement(sb, transformer.apply(subjectNameID.getValue()));
```

**Suspected Issue**: Code calls `getBaseID()` instead of `getNameID()`. For SAML responses with `<saml:NameID>` (not `<saml:BaseID>`), this may return null.

**Note**: This seems unlikely to be broken given reports from others that it works. Further investigation needed to determine:
- Whether `getBaseID()` in Keycloak's SAML DOM library correctly returns NameID
- Whether there's a configuration issue
- Whether our Keycloak version has a bug

### 4. Standard SAML Attribute Importer

**Reason for not working**: The attribute `urn:mace:surf.nl:attribute-def:internal-collabPersonId` is NOT present in the final SAML response to Keycloak. It exists in earlier SAML exchanges but is stripped by the time Keycloak receives it.

## Working Solution: Backfill Script

**Location**: `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keycloak-migration/backfill-sso-attributes.py`

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

**Maintenance**: Schedule to run periodically (e.g., daily cron job) to catch new SSO-Rijk users.

## Token Mapping Configuration

Once user attribute is set (via backfill script), add protocol mapper on RIG client:

**Mapper Configuration**:
- Mapper Type: `User Attribute`
- User Attribute: `sso_rijk_collab_person_id`
- Token Claim Name: `sub` (to override) or custom claim name
- Claim JSON Type: `String`
- Add to ID token: ✅ ON
- Add to access token: ✅ ON
- Add to userinfo: ✅ ON

## Next Steps

### High Priority: Investigate Username Template Importer

The `${NAMEID}` template variable **should** work according to:
1. Keycloak source code shows it's implemented
2. Other users report it works
3. It's the standard way to extract NameID

**Investigation needed**:
1. Check Keycloak version - is there a known bug?
2. Verify SAML response structure matches expectations
3. Enable debug logging for `org.keycloak.broker.saml.mappers.UsernameTemplateMapper`
4. Test with different Target values (`LOCAL`, `BROKER_ID`, `BROKER_USERNAME`)
5. Check if "Email as username" setting interferes
6. Verify no other mappers conflict/override

### Alternative: Custom IDP Mapper

If Username Template Importer truly doesn't work, write custom Keycloak extension:

**Class**: Custom Identity Provider Mapper extending `AbstractIdentityProviderMapper`

**Implementation**:
```java
@Override
public void preprocessFederatedIdentity(KeycloakSession session, RealmModel realm,
        IdentityProviderMapperModel mapperModel, BrokeredIdentityContext context) {

    AssertionType assertion = (AssertionType) context.getContextData().get(SAMLEndpoint.SAML_ASSERTION);
    SubjectType subject = assertion.getSubject();

    if (subject != null && subject.getSubType() != null) {
        NameIDType nameID = subject.getSubType().getNameID();  // Use getNameID() not getBaseID()
        if (nameID != null && nameID.getValue() != null) {
            context.setUserAttribute("sso_rijk_collab_person_id",
                                    Arrays.asList(nameID.getValue()));
        }
    }
}
```

**Deployment**: Package as JAR and place in Keycloak's `providers/` directory.

## References

- Keycloak SAML Endpoint: `services/src/main/java/org/keycloak/broker/saml/SAMLEndpoint.java`
- Username Template Mapper: `services/src/main/java/org/keycloak/broker/saml/mappers/UsernameTemplateMapper.java`
- XPath Attribute Mapper: `services/src/main/java/org/keycloak/broker/saml/mappers/XPathAttributeMapper.java`
- Identity Broker Service: `services/src/main/java/org/keycloak/services/resources/IdentityBrokerService.java`

## Current Status

- ✅ Backfill script implemented and tested
- ✅ Token mapping strategy confirmed (override `sub` claim)
- ⚠️ Username Template Importer with `${NAMEID}` not working - **requires further investigation**
- ❌ No built-in mapper found that extracts NameID to user attributes automatically

**Recommendation**: Investigate Username Template Importer issue as it's the most likely solution. If confirmed broken, either:
1. Report bug to Keycloak project
2. Implement custom mapper extension
3. Accept periodic backfill script approach
