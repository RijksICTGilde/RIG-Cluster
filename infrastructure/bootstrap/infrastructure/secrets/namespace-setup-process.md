# Namespace Secret Setup Process

## TODO: this document is old and needs to be rewritten

This document outlines the process for setting up secrets when adding a new namespace to the RIG Cluster.

## Prerequisites

- Access to the `age` CLI tool for key generation
- Access to the `sops` CLI tool for secret encryption
- Git access to the infrastructure repository
- Kubernetes access to the cluster (for applying bootstrap secrets)

## Process

### 1. Generate Namespace-Specific Age Keys

```bash
# Create keys directory if it doesn't exist
mkdir -p ~/.rig-cluster/keys

# Generate a new Age key pair for the namespace
age-keygen -o ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt

# Extract the public key
PUBLIC_KEY=$(grep "public key" ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt | cut -d' ' -f 4)

# Store the public key in a secure location for team access
echo "Public key for ${NAMESPACE}: ${PUBLIC_KEY}"
```

### 2. Update SOPS Configuration

Add the new namespace to the `.sops.yaml` configuration:

```yaml
# Add to .sops.yaml
- path_regex: secrets/${NAMESPACE}/.*\.yaml$
  age: ${PUBLIC_KEY}
```

### 3. Create the ArgoCD Key Secret

```bash
# Create or update the ArgoCD sops-age secret
# This secret contains all the Age private keys
kubectl get secret sops-age -n rig-system -o yaml > current-sops-age.yaml

# Extract and decode the keys.txt
kubectl get secret sops-age -n rig-system -o jsonpath='{.data.keys\.txt}' | base64 -d > current-keys.txt

# Append the new key
cat ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt >> current-keys.txt

# Create the updated secret
kubectl create secret generic sops-age -n rig-system --from-file=keys.txt=current-keys.txt --dry-run=client -o yaml > new-sops-age.yaml

# Apply the updated secret
kubectl apply -f new-sops-age.yaml

# Clean up
rm current-sops-age.yaml current-keys.txt new-sops-age.yaml
```

### 4. Prepare Database Resources (if needed)

```bash
# Create database and user secrets
cat > db-credentials-${NAMESPACE}.yaml << EOF
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  username: ${NAMESPACE}-user
  password: $(openssl rand -base64 20)
  database: ${NAMESPACE}-db
EOF

# Encrypt the secret
sops --encrypt --in-place db-credentials-${NAMESPACE}.yaml

# Move to the correct location
mkdir -p secrets/${NAMESPACE}
mv db-credentials-${NAMESPACE}.yaml secrets/${NAMESPACE}/
```

### 5. Set Up ArgoCD Access

Create an ArgoCD Application for the namespace:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${NAMESPACE}-app
  namespace: rig-system
spec:
  project: default
  source:
    repoURL: https://github.com/your-org/RIG-Cluster.git
    targetRevision: main
    path: implementation/projects/${NAMESPACE}
    plugin:
      name: sops
  destination:
    server: https://kubernetes.default.svc
    namespace: ${NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
    - CreateNamespace=true
```

### 6. Grant Team Access to Secrets

1. Share the Age private key securely with the team (e.g., via password manager)
2. Document the process for the team to encrypt/decrypt their secrets:

```bash
# Encrypt a secret
sops --encrypt --in-place my-secret.yaml

# Decrypt a secret
sops --decrypt my-secret.yaml
```

## Security Considerations

1. **Key Management**:
   - Store private keys securely
   - Consider rotating keys periodically
   - Use a proper key management system in production

2. **Access Control**:
   - Only give teams access to their namespace's keys
   - Limit the number of people with access to all keys
   - Consider implementing additional RBAC controls

3. **Audit**:
   - Track who has access to which keys
   - Regularly review access permissions
   - Consider using signed commits for secret changes

## Example

Creating a new namespace called "project-analytics":

```bash
NAMESPACE=project-analytics

# Generate key
age-keygen -o ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt
PUBLIC_KEY=$(grep "public key" ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt | cut -d' ' -f 4)

# Update SOPS config
# ... (manual edit of .sops.yaml)

# Update ArgoCD secret
kubectl get secret sops-age -n rig-system -o jsonpath='{.data.keys\.txt}' | base64 -d > current-keys.txt
cat ~/.rig-cluster/keys/namespace-${NAMESPACE}.txt >> current-keys.txt
kubectl create secret generic sops-age -n rig-system --from-file=keys.txt=current-keys.txt --dry-run=client -o yaml | kubectl apply -f -

# Create database credentials
cat > db-credentials-${NAMESPACE}.yaml << EOF
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  username: ${NAMESPACE}-user
  password: $(openssl rand -base64 20)
  database: ${NAMESPACE}-db
EOF

sops --encrypt --in-place db-credentials-${NAMESPACE}.yaml
mkdir -p secrets/${NAMESPACE}
mv db-credentials-${NAMESPACE}.yaml secrets/${NAMESPACE}/

# Create ArgoCD application
# ... (apply ArgoCD application manifest)
```