# Secret Templates

This directory contains Kubernetes Secret templates that are processed by the `generate-secrets-for-cluster` task to create SOPS-encrypted secrets for different cluster environments.

## How It Works

1. **Templates** - YAML files in this directory serve as templates for generating secrets
2. **Annotations** - Fields requiring password generation are marked with `@secret-gen` annotations
3. **Processing** - The `task generate-secrets-for-cluster <cluster-name>` command processes these templates
4. **Encryption** - Secrets are encrypted using SOPS with the AGE key from `security/key.txt`
5. **Output** - Generates SOPS-encrypted `.sops` files in `../config/overlays/<cluster-name>/`

## Adding New Secrets

To add a new secret template:

1. **Create the template file** (e.g., `my-service-secret.yaml`):
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: my-service-credentials
  namespace: rig-system
type: Opaque
stringData:
  USERNAME: myuser
  PASSWORD: "changeMe123!" # @secret-gen:random:16
  API_KEY: "defaultkey" # @secret-gen:random:32
```

2. **Use annotations for fields that need generated passwords**:
   - `# @secret-gen:random:16` - Random password (16 characters)
   - `# @secret-gen:random:20` - Random password (20 characters) 
   - `# @secret-gen:bcrypt:16` - BCrypt hash of random password (16 chars)

3. **Run the generator**:
```bash
task generate-secrets-for-cluster local
task generate-secrets-for-cluster odcn-production
```

## Supported Password Types

- **`random:N`** - Random alphanumeric password of N characters
- **`bcrypt:N`** - BCrypt hash of a random N-character password (for ArgoCD, etc.)

## Important Notes

- Only fields with `@secret-gen` annotations will have passwords generated
- Fields without annotations keep their template values
- The task generates both encrypted secrets and a password overview file
- All secrets must use `stringData` format (not `data`)
- Generated files follow the kustomize overlay pattern

## Generated Structure

After running `task generate-secrets-for-cluster local`:

```
../config/overlays/local/
├── kustomization.yaml          # References decrypt-sops.yaml
├── decrypt-sops.yaml          # Lists all encrypted files  
├── my-service-secret.yaml.sops # Your encrypted secret
└── ...other-secrets.yaml.sops
```

## Password Overview

The task also generates `secrets-overview-<cluster>.txt` in the project root containing all generated passwords for secure storage. This file is automatically gitignored.