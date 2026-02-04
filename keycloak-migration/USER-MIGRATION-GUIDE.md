# Keycloak User Migration - Execution Checklist

Complete checklist for migrating users between Keycloak realms with passwords.

---

## ⚙️ Configuration Variables

**Copy and paste these, then edit the values for your environment:**

```bash
# Digilab (Source)
export SOURCE_CONTEXT="digilab"
export SOURCE_NAMESPACE="tn-ai-validation-keycloak"
export SOURCE_POD="keycloak-dpl-84ccc45bb-6jhr7"
export SOURCE_REALM="algoritmes"
export SOURCE_KEYCLOAK_URL="https://keycloak.apps.digilab.network"

# Local (Target)
export TARGET_CONTEXT="kind-gitops-fluxcd"
export TARGET_NAMESPACE="rig-system"
export TARGET_POD="keycloak-64859cc45f-t7v7s"
export TARGET_REALM="amt-test-migration"
export TARGET_KEYCLOAK_URL="http://keycloak.kind"

# Working directory
export WORK_DIR="$HOME/keycloak-migration-$(date +%Y%m%d)"
mkdir -p $WORK_DIR
cd $WORK_DIR
```

---

## 📋 Migration Checklist

### ☐ Step 1: Prerequisites

```bash
# Verify kubectl access to source cluster
kubectl config use-context $SOURCE_CONTEXT && kubectl get pods -n $SOURCE_NAMESPACE | grep keycloak

# Verify kubectl access to target cluster
kubectl config use-context $TARGET_CONTEXT && kubectl get pods -n $TARGET_NAMESPACE | grep keycloak
```

---

### ☐ Step 2: Backfill SSO Attributes (Source Realm)

**Run the backfill script to ensure all users have SSO attributes:**

```bash
# Dry-run first (recommended)
python /Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keycloak-migration/backfill-sso-attributes.py $SOURCE_KEYCLOAK_URL $SOURCE_REALM admin --dry-run

# If dry-run looks good, run for real
python /Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keycloak-migration/backfill-sso-attributes.py $SOURCE_KEYCLOAK_URL $SOURCE_REALM admin
```

**Verification:**
- Check output shows users updated with `sso-rijk-userid` attribute
- Login to Admin Console and spot-check a few users have the attributes

---

### ☐ Step 3: Export Users from Source

**Switch to source cluster:**

```bash
kubectl config use-context $SOURCE_CONTEXT
```

**Run export command in pod:**

```bash
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- /opt/keycloak/bin/kc.sh export --dir /tmp/export --realm $SOURCE_REALM --users realm_file --users-per-file 999999
```

**Verify export was created:**

```bash
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- ls -lh /tmp/export/
```

Expected output: `<realm>-realm.json` file should exist

**Preview export to verify password hashes are included:**

```bash
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- sh -c "cat /tmp/export/${SOURCE_REALM}-realm.json | head -100"
```

Look for `"credentials"` array with `"hashedSaltedValue"` - this confirms passwords are exported.

---

### ☐ Step 4: Extract Export to Local Machine

```bash
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- cat /tmp/export/${SOURCE_REALM}-realm.json > $WORK_DIR/${SOURCE_REALM}-realm-original.json
```

**Verify extraction:**

```bash
ls -lh $WORK_DIR/
cat $WORK_DIR/${SOURCE_REALM}-realm-original.json | grep -o '"username"' | wc -l
```

**Clean up export from pod:**

```bash
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- rm -rf /tmp/export/
```

---

### ☐ Step 5: Transform JSON (Optional)

F.e. to import only the users and identityproviders... this may require manual search/replace for SSO alias

We also need to remove the service accounts..

```
jq '{realm: .realm, id: .id, enabled: .enabled, users: .users, identityProviders: .identityProviders}' source-export.json > target-import.json
```

**Set the file to import:**

```bash
export IMPORT_FILE="$WORK_DIR/target-import.json"
```

---

### ☐ Step 6: Prepare Target Realm

**Switch to target cluster:**

```bash
kubectl config use-context $TARGET_CONTEXT
```

**Verify target realm exists:**

```bash
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- /opt/keycloak/bin/kc.sh show-config | grep -i realm
```

If realm doesn't exist, create it via Admin Console: `$TARGET_KEYCLOAK_URL/admin`

---

### ☐ Step 7: Import Users to Target


**Create import directory structure:**

```bash
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- bash -c "mkdir -p /tmp/import"
```

**Copy JSON file to target pod:**

```bash
cat $IMPORT_FILE | kubectl exec -i -n $TARGET_NAMESPACE $TARGET_POD -- sh -c 'cat > /tmp/import/users-import.json'
```

**Verify file is in place:**

```bash
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- ls -lh /tmp/import/
```

**Run import (choose one):**

STOP!!
Import from the keycloak cmd line doesn't seem to work (not sure why)..
Also.. I had to remove the service accounts (amt / wies /opi) from the users list... they seem to get
created silently?
But.. the import through the UI an an existing realm (the partial import).. does seem to work and also
does seem to include the passwords... (need to verify)..

```bash
# Option A: Skip existing users (SAFE - recommended first run)
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- /opt/keycloak/bin/kc.sh import --dir /tmp/import --override false

# Option B: Overwrite existing users (DESTRUCTIVE - only if you want to replace)
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- /opt/keycloak/bin/kc.sh import --dir /tmp/import --realm $TARGET_REALM --override true
```

**Clean up import directory:**

```bash
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- rm -rf /tmp/import/
```

---

### ☐ Step 8: Verification

**Count users in export file:**

```bash
cat $IMPORT_FILE | grep -o '"username"' | wc -l
```

**Check target realm user count in Admin Console:**
- Open: `$TARGET_KEYCLOAK_URL/admin/master/console/#/$TARGET_REALM/users`
- Verify user count matches export

**Verify password credentials exist:**
- Select a test user in Admin Console
- Go to **Credentials** tab
- Should show: "Password: Set"

**Verify SSO attributes:**
- Select a user in Admin Console
- Go to **Attributes** tab
- Check for `sso-rijk-userid` and other SSO attributes

**Verify federated identities:**
- Select a user in Admin Console
- Go to **Identity Provider Links** tab
- Check that federated identity is linked correctly

**Test user login:**

```bash
# Try logging in via Keycloak account console
echo "Test login at: $TARGET_KEYCLOAK_URL/realms/$TARGET_REALM/account"
```

Login with a test user's credentials to verify passwords work.

---

## 🔧 Troubleshooting Commands

### Export file not found

```bash
# List all files in export directory
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- ls -la /tmp/export/

# Try different Keycloak binary path
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- find /opt -name "kc.sh" 2>/dev/null
```

### Check Keycloak logs

```bash
# Source cluster logs
kubectl config use-context $SOURCE_CONTEXT
kubectl logs -n $SOURCE_NAMESPACE $SOURCE_POD --tail=100

# Target cluster logs
kubectl config use-context $TARGET_CONTEXT
kubectl logs -n $TARGET_NAMESPACE $TARGET_POD --tail=100
```

### Import fails with "realm not found"

```bash
# List available realms
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- /opt/keycloak/bin/kc.sh show-config | grep realm
```

Create the realm first via Admin Console if it doesn't exist.

### kubectl cp doesn't work

```bash
# Use cat method instead
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- cat /path/to/file > local-file
```

---

## 📝 Quick Summary Commands

**Complete migration in one go (after setting variables):**

```bash
# Switch to source and export
kubectl config use-context $SOURCE_CONTEXT && kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- /opt/keycloak/bin/kc.sh export --dir /tmp/export --realm $SOURCE_REALM --users realm_file --users-per-file 999999

# Extract to local
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- cat /tmp/export/${SOURCE_REALM}-users-0.json > $WORK_DIR/${SOURCE_REALM}-users-original.json

# Clean source pod
kubectl exec -n $SOURCE_NAMESPACE $SOURCE_POD -- rm -rf /tmp/export/

# Transform (if needed)
python $WORK_DIR/transform-users.py $WORK_DIR/${SOURCE_REALM}-users-original.json $WORK_DIR/${TARGET_REALM}-users-transformed.json --remove-ids

# Switch to target and import
kubectl config use-context $TARGET_CONTEXT && kubectl cp $WORK_DIR/${TARGET_REALM}-users-transformed.json $TARGET_NAMESPACE/$TARGET_POD:/tmp/users-import.json

# Prepare and import
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- bash -c "mkdir -p /tmp/import && mv /tmp/users-import.json /tmp/import/${TARGET_REALM}-users-0.json" && kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- /opt/keycloak/bin/kc.sh import --dir /tmp/import --realm $TARGET_REALM --override false

# Clean target pod
kubectl exec -n $TARGET_NAMESPACE $TARGET_POD -- rm -rf /tmp/import/

# Verify count
echo "Exported users:" && cat $WORK_DIR/${TARGET_REALM}-users-transformed.json | grep -o '"username"' | wc -l
```

---

## 🔒 Safety Checklist

Before running the import:

- [ ] Backup target realm (if it has existing data)
- [ ] Test with `--override false` first (skip existing)
- [ ] Verify export file has password credentials (`hashedSaltedValue`)
- [ ] Verify export file has correct user count
- [ ] Have rollback plan ready
- [ ] Document any transformations applied

---

## 📚 Related Documentation

- [backfill-sso-attributes.py](./backfill-sso-attributes.py) - Backfill SSO attributes script
- [migration.md](./migration.md) - Full federation migration guide
- [README.md](./README.md) - Keycloak SSO-Rijk federation overview

---

## Variables Reference

Quick copy-paste for different environments:

### Production → Local Kind

```bash
export SOURCE_CONTEXT="rig-prd-operations/api-prd1-gn2-quattro-rijksapps-nl:6443/robbert.uittenbroek"
export SOURCE_NAMESPACE="keycloak"
export SOURCE_POD="keycloak-0"
export SOURCE_REALM="algoritmes"
export SOURCE_KEYCLOAK_URL="https://keycloak.apps.digilab.network"
export TARGET_CONTEXT="kind-gitops-fluxcd"
export TARGET_NAMESPACE="keycloak"
export TARGET_POD="keycloak-0"
export TARGET_REALM="amt-136-local"
export TARGET_KEYCLOAK_URL="http://keycloak.kind"
export WORK_DIR="$HOME/keycloak-migration-$(date +%Y%m%d)"
```

### Production → Production (Different Realm)

```bash
export SOURCE_CONTEXT="rig-prd-operations/api-prd1-gn2-quattro-rijksapps-nl:6443/robbert.uittenbroek"
export SOURCE_NAMESPACE="keycloak"
export SOURCE_POD="keycloak-0"
export SOURCE_REALM="algoritmes"
export SOURCE_KEYCLOAK_URL="https://keycloak.apps.digilab.network"
export TARGET_CONTEXT="rig-prd-operations/api-prd1-gn2-quattro-rijksapps-nl:6443/robbert.uittenbroek"
export TARGET_NAMESPACE="keycloak-new"
export TARGET_POD="keycloak-0"
export TARGET_REALM="new-realm"
export TARGET_KEYCLOAK_URL="https://keycloak-new.apps.digilab.network"
export WORK_DIR="$HOME/keycloak-migration-$(date +%Y%m%d)"
```

---

## ✅ Final Verification Checklist

After import completion:

- [ ] User count matches between source and target
- [ ] Test user can login with existing password
- [ ] SSO attributes present (`sso-rijk-userid`, etc.)
- [ ] Federated identities linked correctly
- [ ] End-to-end SSO flow works (if applicable)
- [ ] No errors in Keycloak logs
- [ ] Export files saved for reference
- [ ] Document what was migrated (date, user count, any issues)

**Migration complete! 🎉**
