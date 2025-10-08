# RIG Cluster Bootstrap Process

This document outlines the complete bootstrap process for the RIG Cluster infrastructure. It provides a step-by-step guide for setting up the core infrastructure components using ArgoCD and Kustomize.

## Overview

The bootstrap process establishes the foundation for a GitOps-based infrastructure deployment. It follows these key principles:

- **Minimal Initial Setup**: Only deploy what's necessary to bootstrap the cluster
- **GitOps-Driven**: All subsequent deployments are managed through ArgoCD
- **Environment Flexibility**: Support for local development, GitHub-based workflows, and production environments
- **Secure by Default**: Proper secret management from the beginning

## Prerequisites

- Kubernetes cluster with admin access
- Command-line tools:
  - kubectl
  - kustomize
  - task (from taskfile.dev)
  - git (for source control)
  - kubeseal (for sealed secrets, if used)

## Bootstrap Process

### 1. Repository Structure

The bootstrap configuration is organized as follows:

```
implementation/bootstrap/
├── BOOTSTRAP.md         # Detailed bootstrap documentation
├── README.md            # This overview document
├── clusters/            # Cluster-specific configurations
│   ├── local/           # Local development cluster
│   ├── minimal/         # Minimal production setup
│   └── odcn/            # ODCN environment with network policies
├── infrastructure/      # Core infrastructure components
    ├── argocd/          # ArgoCD deployment and configuration
    ├── common/          # Common resources shared across components
    ├── keycloak/        # Keycloak identity provider
    ├── minio/           # MinIO object storage
    ├── postgresql/      # PostgreSQL database service
    ├── secrets/         # Secret management templates and scripts
    └── vault/           # HashiCorp Vault secret management
```

### 2. Bootstrap Steps

#### Step 1: Prepare the Environment

1. Clone the repository:
   ```bash
   git clone https://github.com/your-org/rig-cluster.git
   cd rig-cluster
   ```

2. For local development, create a Kind cluster:
   ```bash
   task create-k8s-cluster
   ```

#### Step 2: Initialize Secret Management (Optional)

For environments requiring secure credentials:

1. Generate sealed secrets:
   ```bash
   cd implementation/bootstrap/infrastructure/secrets
   ./generate-sealed-secrets.sh <cluster-name>
   ```

2. Securely store the generated credentials and apply the sealed secrets:
   ```bash
   kubectl apply -f implementation/bootstrap/infrastructure/secrets/sealed/<cluster-name>/
   ```

#### Step 3: Bootstrap ArgoCD

The bootstrap process establishes ArgoCD, which manages all subsequent deployments:

1. For minimal setup (creates namespace and deploys ArgoCD):
   ```bash
   task bootstrap-minimal
   ```

2. For different environments:
   ```bash
   # Local development with filesystem source
   task bootstrap-minimal SOURCE_TYPE=local-filesystem
   
   # GitHub-based deployment
   task bootstrap-minimal SOURCE_TYPE=github
   
   # ODCN environment with network policies
   task bootstrap-minimal SOURCE_TYPE=odcn
   ```

3. To preview what will be applied (dry run):
   ```bash
   task bootstrap-minimal DRY_RUN=true
   ```

#### Step 4: Access ArgoCD

1. Port-forward the ArgoCD service:
   ```bash
   kubectl port-forward svc/argocd-server -n rig-system 8080:80
   ```

2. Open http://localhost:8080 in your browser
   - Default credentials: admin/admin
   - For production, the password is in the sealed secrets or can be retrieved with:
     ```bash
     kubectl get secret argocd-secret -n rig-system -o jsonpath="{.data.admin\.password}" | base64 -d
     ```

#### Step 5: Verify the Deployment

1. Check the ArgoCD application status:
   ```bash
   kubectl get applications -n rig-system
   ```

2. Monitor the ArgoCD sync process through the UI or with:
   ```bash
   kubectl get applications.argoproj.io -n rig-system -o jsonpath='{.items[].status.sync.status}'
   ```

3. Verify the core infrastructure components are being deployed:
   ```bash
   kubectl get pods -n rig-system
   ```

### 3. Post-Bootstrap Configuration

After the bootstrap process completes, ArgoCD will manage the deployment of additional infrastructure components:

1. **PostgreSQL**: Database service for application data
2. **Keycloak**: Identity and access management
3. **Vault**: Secret management
4. **MinIO**: Object storage service

#### Accessing Services

- **PostgreSQL**:
  ```bash
  kubectl port-forward svc/postgresql -n rig-system 5432:5432
  ```

- **Keycloak**:
  ```bash
  kubectl port-forward svc/keycloak -n rig-system 8081:8080
  ```

- **Vault**:
  ```bash
  kubectl port-forward svc/vault -n rig-system 8200:8200
  ```

- **MinIO**:
  ```bash
  kubectl port-forward svc/minio -n rig-system 9000:9000 9001:9001
  ```

## Customization

### Environment-Specific Configuration

Each environment type has specific customization options:

1. **Local Development**:
   - Uses local filesystem for ArgoCD source
   - Simplified resource requirements
   - No network policies
   - Local storage classes

2. **GitHub-Based Workflow**:
   - Uses GitHub repository as ArgoCD source
   - Supports CI/CD integration
   - Can use different branches for different environments

3. **ODCN Environment**:
   - Includes network policies for enhanced security
   - Production-ready resource configurations
   - Integration with ODCN-specific resources

### Modifying the Bootstrap Configuration

To customize the bootstrap process:

1. Edit the appropriate kustomization files in your environment directory:
   ```bash
   implementation/bootstrap/clusters/<environment>/kustomization.yaml
   ```

2. Add or modify patches for specific components:
   ```bash
   implementation/bootstrap/infrastructure/<component>/overlays/<environment>/
   ```

3. Test your changes with a dry run:
   ```bash
   task bootstrap-minimal SOURCE_TYPE=<environment> DRY_RUN=true
   ```

## Troubleshooting

### Common Issues

1. **ArgoCD fails to start**: 
   - Check for missing secrets: `kubectl get secret argocd-secret -n rig-system`
   - Verify the ArgoCD operator is deployed: `kubectl get pods -n rig-system | grep argocd-operator`
   - Check operator logs: `kubectl logs deployment/argocd-operator -n rig-system`

2. **Applications not syncing**:
   - Check application status: `kubectl describe application -n rig-system`
   - Verify repository access in ArgoCD settings
   - Check for issues in the kustomization files

3. **Network policy issues**:
   - Temporarily disable network policies for troubleshooting
   - Check pod connectivity with: `kubectl exec -it <pod> -n rig-system -- ping <service>`

### Getting Help

For additional assistance, check the following resources:

- ArgoCD documentation: https://argoproj.github.io/argo-cd/
- Repository issues: https://github.com/your-org/rig-cluster/issues
- ODCN support channels (for ODCN environments)

## Next Steps

After successfully bootstrapping the cluster:

1. Deploy application workloads through ArgoCD
2. Configure additional services as needed
3. Set up monitoring and logging
4. Establish backup and disaster recovery procedures

For detailed information on these steps, refer to the main project documentation.