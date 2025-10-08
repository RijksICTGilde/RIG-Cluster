# Keycloak SSO-Rijk Federation Migration Plan

## Goal

Create a transparent pass-through chain where SSO-Rijk attributes flow from:
**SSO-Rijk** → **Digilab Keycloak** → **RIG Keycloak** → **Applications**

Users should maintain their original SSO-Rijk identity (userId/userName) throughout the chain, even when moving through multiple Keycloak instances.

---

## SSO-Rijk SAML Attributes Discovery

### Critical Identity Attributes

| SAML Attribute Name | Example Value | Purpose |
|---|---|---|
| `urn:mace:surf.nl:attribute-def:internal-collabPersonId` | `urn:collab:person:minbzk.nl:Uittenbroek` | **PRIMARY USER ID** - Used for `federatedIdentities.userId` |
| `urn:oasis:names:tc:SAML:attribute:subject-id` | `5701035859@minbzk.nl` | Alternative unique ID |
| `urn:oasis:names:tc:SAML:attribute:pairwise-id` | `0aa81dd859584f4e22afaadcb8aa22873ab0900d2573c9d9aba4be7ae69e64bf@minbzk.nl` | Pairwise unique ID |
| `urn:rijksoverheid:federation:personalUniqueCode` | `urn:idm:5701035859` | Personal unique code |

### Personal Information Attributes

| SAML Attribute Name | Example Value |
|---|---|
| `urn:rijksoverheid:federation:surName` | `Uittenbroek` |
| `urn:rijksoverheid:federation:givenName` | `Robbert` |
| `urn:rijksoverheid:federation:displayName` | `Uittenbroek, Robbert` |
| `urn:rijksoverheid:federation:uid` | `Uittenbroek` |
| `urn:rijksoverheid:federation:userPrincipalName` | `robbert.uittenbroek@rijksoverheid.nl` |
| `urn:rijksoverheid:federation:emailAddress` | `Robbert.Uittenbroek@rijksoverheid.nl` |

### Organization Attributes

| SAML Attribute Name | Example Value |
|---|---|
| `urn:rijksoverheid:federation:organization` | `minbzk.nl` |
| `urn:rijksoverheid:federation:organizationDisplayName` | `Ministerie van Binnenlandse Zaken en Koninkrijksrelaties` |
| `urn:rijksoverheid:federation:organizationNumber` | `urn:oin:00000004000000059000` |

### Authentication Context (Optional)

| SAML Attribute Name | Example Value |
|---|---|
| `urn:rijksoverheid:federation:idContext` | `urn:rijksoverheid:federation:idContext:loa2` |
| `urn:rijksoverheid:federation:authnContext` | `urn:rijksoverheid:federation:authnContext:loa1` |
| `urn:rijksoverheid:federation:entitlement` | (multiple values - Azure GUIDs, authorizations) |

### Key Finding: userId vs userName

Keycloak creates `federatedIdentities` with:
- **userId**: `urn:collab:person:minbzk.nl:Uittenbroek` (original case preserved)
- **userName**: `urn:collab:person:minbzk.nl:uittenbroek` (automatically lowercased)

Both come from `urn:mace:surf.nl:attribute-def:internal-collabPersonId` - Keycloak normalizes the username to lowercase.

---

## Migration Overview

### Current Problem

```
SSO-Rijk SAML
  └─> Digilab Keycloak
        ├─> federatedIdentities stored (userId: urn:collab:person:...)
        └─> OIDC to RIG Keycloak
              └─> federatedIdentities created (userId: d29542a5-... ❌ WRONG)
```

The SSO-Rijk identity is **lost** when users authenticate via Digilab to RIG.

### Desired Solution

```
SSO-Rijk SAML (urn:collab:person:minbzk.nl:Uittenbroek)
  └─> Digilab Keycloak
        ├─> Store as user attributes (sso_rijk_*)
        └─> OIDC token with custom 'sub' claim
              └─> RIG Keycloak
                    └─> federatedIdentities (userId: urn:collab:person:... ✅ CORRECT)
```

---

## Migration Steps

### Phase 1: Configure Digilab Keycloak

#### 1.1 Add SAML Attribute Mappers to sso-rijk IDP

**Location**: Digilab Keycloak → Realm: `algoritmes` → Identity Providers → `sso-rijk` → Mappers

Create mappers to capture SSO-Rijk SAML attributes as Keycloak user attributes.

**How to Add a Mapper:**
1. Login to Digilab Keycloak Admin Console
2. Select Realm: `algoritmes`
3. Navigate to: **Identity providers** (left menu)
4. Click on: **sso-rijk**
5. Click the **Mappers** tab
6. Click **Add mapper** button
7. Fill in the fields as specified below
8. Click **Save**

**Mapper 1: Primary Identity (CRITICAL)**
- **Name**: `sso-rijk-collab-person-id`
- **Sync Mode Override**: `FORCE`
- **Mapper Type**: `Attribute Importer` (select from dropdown)
- **Attribute Name**: `urn:mace:surf.nl:attribute-def:internal-collabPersonId`
- **Attribute Name Format**: `URI Reference` or `URI` (select from dropdown)
- **User Attribute Name**: `sso_rijk_collab_person_id`

**⚠️ IMPORTANT**: This mapper will only apply to NEW logins. For existing users, run the backfill script first (see Phase 1.1.1 below).

**Mapper 2-11: All Other Attributes**

For each attribute below, create an `Attribute Importer` mapper:

| Mapper Name | SAML Attribute | User Attribute Name | Sync Mode |
|---|---|---|---|
| `sso-rijk-surname` | `urn:rijksoverheid:federation:surName` | `sso_rijk_surname` | `INHERIT` |
| `sso-rijk-given-name` | `urn:rijksoverheid:federation:givenName` | `sso_rijk_given_name` | `INHERIT` |
| `sso-rijk-display-name` | `urn:rijksoverheid:federation:displayName` | `sso_rijk_display_name` | `INHERIT` |
| `sso-rijk-uid` | `urn:rijksoverheid:federation:uid` | `sso_rijk_uid` | `INHERIT` |
| `sso-rijk-upn` | `urn:rijksoverheid:federation:userPrincipalName` | `sso_rijk_upn` | `INHERIT` |
| `sso-rijk-email` | `urn:rijksoverheid:federation:emailAddress` | `sso_rijk_email` | `INHERIT` |
| `sso-rijk-org` | `urn:rijksoverheid:federation:organization` | `sso_rijk_org` | `INHERIT` |
| `sso-rijk-org-display` | `urn:rijksoverheid:federation:organizationDisplayName` | `sso_rijk_org_display_name` | `INHERIT` |
| `sso-rijk-org-number` | `urn:rijksoverheid:federation:organizationNumber` | `sso_rijk_org_number` | `INHERIT` |
| `sso-rijk-subject-id` | `urn:oasis:names:tc:SAML:attribute:subject-id` | `sso_rijk_subject_id` | `INHERIT` |
| `sso-rijk-unique-code` | `urn:rijksoverheid:federation:personalUniqueCode` | `sso_rijk_personal_unique_code` | `INHERIT` |

**All mappers use:**
- **Mapper Type**: `Attribute Importer`
- **Attribute Name Format**: `URI Reference` or `URI`
- **Sync Mode Override**: `INHERIT` (or `FORCE` for critical attributes)

#### 1.1.1 Backfill Existing Users (REQUIRED)

**⚠️ Run this BEFORE adding the mappers above, or AFTER adding them but BEFORE proceeding to Phase 1.2**

Since IDP mappers only execute during login, existing users won't have the `sso_rijk_collab_person_id` attribute. Use the backfill script to populate it from their existing `federatedIdentities`.

**Prerequisites:**
```bash
pip install requests
```

**Usage:**
```bash
cd keycloak-migration

python backfill-sso-attributes.py https://keycloak.apps.digilab.network algoritmes admin
# Enter admin password when prompted
```

**What it does:**
- Fetches all users with SSO-Rijk federated identity
- Extracts the `userId` from their `federatedIdentities`
- Sets it as user attribute `sso_rijk_collab_person_id`
- Shows progress and summary

**Expected output:**
```
[1/150] robbert.uittenbroek... ✅ Set sso_rijk_collab_person_id = urn:collab:person:minbzk.nl:Uittenbroek
[2/150] john.doe... ✅ Set sso_rijk_collab_person_id = urn:collab:person:minbzk.nl:Doe
[3/150] jane.smith... ⏭️  Already has sso_rijk_collab_person_id
...
✅ Successfully backfilled 148 user(s)
```

**Verification:**
1. Go to Users in Digilab Admin Console
2. Select a user with SSO-Rijk identity
3. Go to Attributes tab
4. Verify `sso_rijk_collab_person_id` attribute exists

#### 1.2 Override OIDC `sub` Claim for RIG Client

**Location**: Digilab Keycloak → Clients → (client used by RIG) → Client Scopes → Dedicated Scope → Mappers

**CRITICAL: This makes RIG use SSO-Rijk identity instead of Keycloak UUID**

**How to Add Client Protocol Mappers:**

1. Login to Digilab Keycloak Admin Console
2. Select Realm: `algoritmes`
3. Navigate to: **Clients** (left menu)
4. Find and click on the client that RIG uses to connect (e.g., `rig-client` or similar)
5. Click the **Client scopes** tab
6. Under "Assigned client scopes", find the client's **dedicated scope** (usually named like `rig-client-dedicated`)
7. Click on the dedicated scope name
8. Click the **Mappers** tab
9. Click **Add mapper** → **By configuration**
10. Select **User Attribute** from the list
11. Fill in the fields as specified below
12. Click **Save**

**Mapper 1: Override `sub` Claim (CRITICAL)**
- **Name**: `override-sub-with-sso-rijk-id`
- **Mapper Type**: `User Attribute` (auto-selected)
- **User Attribute**: `sso_rijk_collab_person_id`
- **Token Claim Name**: `sub`
- **Claim JSON Type**: `String` (select from dropdown)
- **Add to ID token**: ✅ ON
- **Add to access token**: ✅ ON
- **Add to userinfo**: ✅ ON
- **Add to lightweight access token**: ❌ OFF (optional)
- **Add to token introspection**: ❌ OFF (optional)

**Repeat steps 9-12 for Mapper 2:**

**Mapper 2: Override `preferred_username` Claim**
- **Name**: `override-username-with-sso-rijk`
- **Mapper Type**: `User Attribute`
- **User Attribute**: `sso_rijk_collab_person_id`
- **Token Claim Name**: `preferred_username`
- **Claim JSON Type**: `String`
- **Add to ID token**: ✅ ON
- **Add to access token**: ✅ ON
- **Add to userinfo**: ❌ OFF (optional)

**⚠️ IMPORTANT NOTES**:
- Keycloak will automatically lowercase the `preferred_username` value
- The `sub` claim override is what makes RIG create `federatedIdentities` with the SSO-Rijk ID instead of a UUID
- If you don't see "User Attribute" in the mapper type list, make sure you selected "By configuration" when adding the mapper

**Verification:**
After adding these mappers, test with a user:
1. Have a user login via SSO-Rijk
2. In Digilab Admin Console → Clients → (your client) → Client Scopes → Evaluate
3. Select a user with SSO-Rijk identity
4. Click "Generated ID Token"
5. Check that `"sub"` shows `urn:collab:person:minbzk.nl:...` instead of a UUID

#### 1.3 Add All SSO-Rijk Attributes to OIDC Token

**Location**: Same as 1.2

Create `User Attribute` mappers for all SSO-Rijk attributes:

| Mapper Name | User Attribute | Token Claim Name |
|---|---|---|
| `claim-sso-rijk-collab-id` | `sso_rijk_collab_person_id` | `sso_rijk_collab_person_id` |
| `claim-sso-rijk-surname` | `sso_rijk_surname` | `sso_rijk_surname` |
| `claim-sso-rijk-given-name` | `sso_rijk_given_name` | `sso_rijk_given_name` |
| `claim-sso-rijk-display-name` | `sso_rijk_display_name` | `sso_rijk_display_name` |
| `claim-sso-rijk-uid` | `sso_rijk_uid` | `sso_rijk_uid` |
| `claim-sso-rijk-upn` | `sso_rijk_upn` | `sso_rijk_upn` |
| `claim-sso-rijk-email` | `sso_rijk_email` | `sso_rijk_email` |
| `claim-sso-rijk-org` | `sso_rijk_org` | `sso_rijk_org` |
| `claim-sso-rijk-org-display` | `sso_rijk_org_display_name` | `sso_rijk_org_display_name` |
| `claim-sso-rijk-org-number` | `sso_rijk_org_number` | `sso_rijk_org_number` |
| `claim-sso-rijk-subject-id` | `sso_rijk_subject_id` | `sso_rijk_subject_id` |
| `claim-sso-rijk-unique-code` | `sso_rijk_personal_unique_code` | `sso_rijk_personal_unique_code` |

**All mappers:**
- **Mapper Type**: `User Attribute`
- **Claim JSON Type**: `String`
- **Add to ID token**: ✅ ON
- **Add to access token**: ✅ ON
- **Add to userinfo**: ✅ ON

---

### Phase 2: Configure RIG Keycloak

#### 2.1 Import SSO-Rijk Attributes from Digilab IDP

**Location**: RIG Keycloak → Realm: `RIG` → Identity Providers → `digilab` (or whatever you call it) → Mappers

**IMPORTANT**: The `sub` and `preferred_username` claims are automatically used by Keycloak for `federatedIdentities.userId` and `userName`. No mapper needed for those!

**Create Attribute Importers for all other claims:**

| Mapper Name | Claim Name | User Attribute Name |
|---|---|---|
| `import-sso-rijk-collab-id` | `sso_rijk_collab_person_id` | `sso_rijk_collab_person_id` |
| `import-sso-rijk-surname` | `sso_rijk_surname` | `sso_rijk_surname` |
| `import-sso-rijk-given-name` | `sso_rijk_given_name` | `sso_rijk_given_name` |
| `import-sso-rijk-display-name` | `sso_rijk_display_name` | `sso_rijk_display_name` |
| `import-sso-rijk-uid` | `sso_rijk_uid` | `sso_rijk_uid` |
| `import-sso-rijk-upn` | `sso_rijk_upn` | `sso_rijk_upn` |
| `import-sso-rijk-email` | `sso_rijk_email` | `sso_rijk_email` |
| `import-sso-rijk-org` | `sso_rijk_org` | `sso_rijk_org` |
| `import-sso-rijk-org-display` | `sso_rijk_org_display_name` | `sso_rijk_org_display_name` |
| `import-sso-rijk-org-number` | `sso_rijk_org_number` | `sso_rijk_org_number` |
| `import-sso-rijk-subject-id` | `sso_rijk_subject_id` | `sso_rijk_subject_id` |
| `import-sso-rijk-unique-code` | `sso_rijk_personal_unique_code` | `sso_rijk_personal_unique_code` |

**All mappers:**
- **Mapper Type**: `Attribute Importer`
- **Sync Mode**: `INHERIT`

#### 2.2 Create Client Scope for SSO-Rijk Attributes

**Location**: RIG Keycloak → Client Scopes → Create

**Create a reusable client scope:**
- **Name**: `sso-rijk-attributes`
- **Type**: `Default` (to automatically include in all clients)
- **Protocol**: `openid-connect`
- **Display On Consent Screen**: OFF
- **Include In Token Scope**: ON

**Add Protocol Mappers to this scope:**

Same as Phase 1.3, but in RIG realm. Create `User Attribute` mappers for all SSO-Rijk attributes.

#### 2.3 Assign Client Scope to Applications

**Location**: RIG Keycloak → Clients → (your application) → Client Scopes

- Add `sso-rijk-attributes` to **Default Client Scopes**

This ensures all applications receive SSO-Rijk attributes in their tokens.

---

### Phase 3: Validation

#### 3.1 Test the Flow

1. **Login to RIG via Digilab IDP** using SSO-Rijk credentials
2. **Check the user in RIG Admin Console**:
   - Navigate to: Users → (find your user) → Identity Provider Links tab
   - **Verify `federatedIdentities`**:
     ```json
     {
       "identityProvider": "digilab",
       "userId": "urn:collab:person:minbzk.nl:Uittenbroek",
       "userName": "urn:collab:person:minbzk.nl:uittenbroek"
     }
     ```
   - ✅ If userId starts with `urn:collab:person:`, SUCCESS!
   - ❌ If userId is a UUID like `d29542a5-...`, the `sub` override didn't work

3. **Check user attributes**:
   - Navigate to: Users → (find your user) → Attributes tab
   - Verify all `sso_rijk_*` attributes are present

4. **Check OIDC Token Claims**:
   - Use an OIDC debugger or decode the ID token from your application
   - Verify all `sso_rijk_*` claims are present

#### 3.2 Troubleshooting

**If userId is still a UUID:**
- Check Digilab client mappers - ensure `sub` override mapper exists
- Check mapper is enabled and has correct user attribute name
- Try using a Script Mapper instead (see Alternative Approaches below)

**If attributes are missing:**
- Check IDP mappers in RIG are enabled
- Check sync mode is not blocking updates
- Try forcing a re-sync by removing and re-adding the IDP link

---

### Phase 4: User Migration (Optional)

**If you need to migrate existing users from Digilab realm to RIG realm:**

#### 4.1 Export Users from Digilab

```bash
# Using Keycloak Admin API
kcadm.sh config credentials --server https://keycloak.apps.digilab.network \
  --realm master --user admin

kcadm.sh get users -r algoritmes > digilab-users.json
```

#### 4.2 Modify User Export

Create script: `keycloak-migration/migrate-users.py`

```python
#!/usr/bin/env python3
import json
import sys

def migrate_user(user):
    """Update federatedIdentities IDP alias"""
    if "federatedIdentities" in user:
        for fed_id in user["federatedIdentities"]:
            if fed_id.get("identityProvider") == "sso-rijk":
                # Change to new IDP alias in RIG
                fed_id["identityProvider"] = "digilab"
    return user

# Read, process, write
with open(sys.argv[1], 'r') as f:
    users = json.load(f)

migrated = [migrate_user(u) for u in users]

with open(sys.argv[1].replace('.json', '-migrated.json'), 'w') as f:
    json.dump(migrated, f, indent=2)
```

Usage:
```bash
python migrate-users.py digilab-users.json
# Creates: digilab-users-migrated.json
```

#### 4.3 Import to RIG

```bash
kcadm.sh config credentials --server https://keycloak.rig.network \
  --realm master --user admin

# Import users
cat digilab-users-migrated.json | jq -c '.[]' | while read user; do
  echo "$user" | kcadm.sh create users -r RIG -f -
done
```

---

## Alternative Approaches

### If `sub` Override Doesn't Work: Use Script Mapper

**Location**: Digilab Keycloak → Clients → RIG Client → Mappers

**Create Script Mapper:**
- **Name**: `script-sub-from-sso-rijk`
- **Mapper Type**: `Script Mapper`
- **Script**:
  ```javascript
  var ssoRijkId = user.getFirstAttribute('sso_rijk_collab_person_id');
  exports = ssoRijkId != null ? ssoRijkId : user.id;
  ```
- **Token Claim Name**: `sub`
- **Claim JSON Type**: `String`
- **Add to ID token**: ✅ ON
- **Multivalued**: ❌ OFF

### Direct SSO-Rijk Connection

Instead of chaining through Digilab, configure RIG to connect directly to SSO-Rijk:

**Pros:**
- Simpler architecture
- Fewer hops
- No `sub` override needed

**Cons:**
- Duplicate IDP configuration
- Lose centralized user management

**When to use:** If Digilab is being phased out.

---

## Protocol Agnostic Design

The user attribute strategy ensures SAML ↔ OIDC changes don't break the chain:

```
SSO-Rijk (SAML Attribute)
  → Keycloak (User Attribute - protocol agnostic)
    → OIDC Claim
      → Downstream Keycloak (User Attribute)
        → OIDC Claim
          → Application
```

If you switch from SAML to OIDC or vice versa:
- **User attributes stay the same**
- **Only mapper type changes** (SAML Attribute Importer → OIDC Attribute Importer)
- **Token claims stay the same**
- **Applications unchanged**

---

## Summary Checklist

### Phase 1: Digilab
- [ ] Add SAML attribute mappers for SSO-Rijk IDP
- [ ] Override `sub` claim in RIG client with `sso_rijk_collab_person_id`
- [ ] Override `preferred_username` claim
- [ ] Add all SSO-Rijk attributes as OIDC claims

### Phase 2: RIG
- [ ] Add OIDC attribute importers for Digilab IDP
- [ ] Create `sso-rijk-attributes` client scope
- [ ] Add protocol mappers to client scope
- [ ] Assign scope to application clients

### Phase 3: Validation
- [ ] Test login via Digilab → RIG
- [ ] Verify `federatedIdentities` has correct userId
- [ ] Verify all attributes present
- [ ] Verify OIDC tokens contain SSO-Rijk claims

### Phase 4: Migration (Optional)
- [ ] Export users from Digilab
- [ ] Run migration script
- [ ] Import users to RIG
- [ ] Validate user linking

---

## Expected Result

After migration, when a user logs in via SSO-Rijk → Digilab → RIG:

**In RIG Keycloak User:**
```json
{
  "username": "urn:collab:person:minbzk.nl:uittenbroek",
  "federatedIdentities": [{
    "identityProvider": "digilab",
    "userId": "urn:collab:person:minbzk.nl:Uittenbroek",
    "userName": "urn:collab:person:minbzk.nl:uittenbroek"
  }],
  "attributes": {
    "sso_rijk_collab_person_id": "urn:collab:person:minbzk.nl:Uittenbroek",
    "sso_rijk_email": "Robbert.Uittenbroek@rijksoverheid.nl",
    "sso_rijk_given_name": "Robbert",
    "sso_rijk_surname": "Uittenbroek",
    ...
  }
}
```

**In OIDC ID Token for Applications:**
```json
{
  "sub": "urn:collab:person:minbzk.nl:uittenbroek",
  "preferred_username": "urn:collab:person:minbzk.nl:uittenbroek",
  "email": "Robbert.Uittenbroek@rijksoverheid.nl",
  "sso_rijk_collab_person_id": "urn:collab:person:minbzk.nl:Uittenbroek",
  "sso_rijk_email": "Robbert.Uittenbroek@rijksoverheid.nl",
  "sso_rijk_given_name": "Robbert",
  "sso_rijk_surname": "Uittenbroek",
  "sso_rijk_display_name": "Uittenbroek, Robbert",
  "sso_rijk_upn": "robbert.uittenbroek@rijksoverheid.nl",
  "sso_rijk_org": "minbzk.nl",
  "sso_rijk_org_display_name": "Ministerie van Binnenlandse Zaken en Koninkrijksrelaties",
  "sso_rijk_org_number": "urn:oin:00000004000000059000",
  ...
}
```

✅ **Complete transparent pass-through achieved!**
