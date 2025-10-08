# Bootstrap Secrets Templates

This directory contains Kubernetes secret templates for bootstrap components (ArgoCD, etc.) that need to be available before the main GitOps infrastructure is running.

## How It Works

Templates use **@secret-gen annotations** to specify which fields need password generation:

```yaml
# Generate a random 16-character password
password: "PLACEHOLDER_VALUE" # @secret-gen:random:16

# Generate a bcrypt hash from a 16-character password  
admin.password: "PLACEHOLDER_BCRYPT_HASH" # @secret-gen:bcrypt:16

# Skip password generation (use existing value)
token: "INSERT_TOKEN_HERE" # @secret-gen:skip
```

## Usage

Run the secret generation task for your target cluster:

```bash
task generate-bootstrap-secrets-for-cluster
```

This will:
1. Generate new passwords where annotated
2. Encrypt secrets with SOPS using `security/key.txt`
3. Output encrypted files to `bootstrap/rig-system/kustomize/overlays/{cluster}/`
4. Create an overview file with plaintext passwords for manual storage

## Adding New Secrets

1. Create a new `.yaml` template in this directory
2. Add `@secret-gen:` annotations for fields that need password generation
3. Use placeholder values that are safe to commit
4. The generation task will automatically include the new template

## Security Notes

- ⚠️  **Templates use placeholder values** - never commit actual passwords!
- 🔐 SOPS encryption uses the AGE key from `security/key.txt`
- 📋 Generated overview files contain plaintext passwords - copy and delete them manually
- 🎯 Path annotations help document where secrets will be deployed
