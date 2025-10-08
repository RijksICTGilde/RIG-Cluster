# Bootstrap Process for RIG Cluster

This document outlines the process for bootstrapping a new RIG Cluster instance with secure admin credentials.

## Prerequisites

- Access to the Kubernetes cluster
- `kubectl` CLI tool
- `kubeseal` CLI tool (for SealedSecrets)
- `git` for repository management

## Credential Management

### Initial Setup

1. **Generate Sealed Secrets**:

   ```bash
   cd implementation/bootstrap/infrastructure/secrets
   ./generate-sealed-secrets.sh <cluster-name>
   ```

   This creates sealed secrets for:
   - PostgreSQL admin credentials
   - Keycloak admin credentials
   - Keycloak database credentials
   - ArgoCD admin credentials
   - Vault initialization credentials

2. **Store Credentials Securely**:

   The script generates a `.credentials-<cluster-name>.txt` file with plaintext credentials.
   - Store this file in a secure location (e.g., password manager)
   - Delete the file once securely stored
   - Never commit this file to Git

3. **Commit the Sealed Secrets**:

   ```bash
   git add implementation/bootstrap/infrastructure/secrets/sealed/<cluster-name>/
   git commit -m "Add sealed secrets for <cluster-name>"
   git push
   ```

### Bootstrapping a New Cluster

1. **Create the rig-system namespace**:

   ```bash
   kubectl create namespace rig-system
   ```

2. **Apply the Sealed Secrets**:

   ```bash
   kubectl apply -f implementation/bootstrap/infrastructure/secrets/sealed/<cluster-name>/
   ```

3. **Configure ArgoCD**:

   ```bash
   # Update the bootstrap script with your specific version
   ./bootstrap-community-cluster.sh
   ```

### Vault Initialization

Vault requires special handling for initialization:

1. After Vault is deployed, initialize it:

   ```bash
   # Port-forward to Vault service
   kubectl port-forward svc/vault -n rig-system 8200:8200
   
   # Initialize Vault
   export VAULT_ADDR=http://localhost:8200
   vault operator init -key-shares=3 -key-threshold=2
   ```

2. Save the unseal keys and root token in the Vault secret:

   ```bash
   kubectl create secret generic vault-init-credentials \
     -n rig-system \
     --from-literal=root-token=<root-token> \
     --from-literal=unseal-key-1=<key-1> \
     --from-literal=unseal-key-2=<key-2> \
     --from-literal=unseal-key-3=<key-3> \
     --dry-run=client -o yaml | kubeseal > vault-init-sealed.yaml
   
   kubectl apply -f vault-init-sealed.yaml
   ```

## Credential Rotation

1. **Generate New Secrets**:

   ```bash
   ./generate-sealed-secrets.sh <cluster-name>-rotation
   ```

2. **Apply the New Secrets**:

   ```bash
   kubectl apply -f implementation/bootstrap/infrastructure/secrets/sealed/<cluster-name>-rotation/
   ```

3. **Restart the Components** (if required):

   ```bash
   kubectl rollout restart deployment/keycloak -n rig-system
   # Note: Some components might require specific rotation procedures
   ```

## Security Considerations

1. **Sealed Secrets Controller**:
   - The controller should be deployed and operational before applying sealed secrets
   - Back up the controller's private key for disaster recovery

2. **External Secret Systems**:
   - For production environments, consider using Vault or External Secrets Operator
   - This provides better secret management and rotation capabilities

3. **Secret Distribution**:
   - Use secure channels to distribute initial credentials to administrators
   - Document credential access in your security policies