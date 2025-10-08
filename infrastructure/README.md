# RIG-Cluster Implementation

This directory contains the implementation of the RIG Cluster components. The directory structure follows a kustomize-based approach to Kubernetes resource management.

## Directory Structure

- `bootstrap/`: Core infrastructure components and configuration
  - `clusters/`: Cluster-specific kustomizations
    - `local/`: Configuration for local development cluster
    - `minimal/`: Minimal configuration for testing
    - `odcn/`: Configuration for ODCN environment
  - `infrastructure/`: Individual component configurations
    - `common/`: Shared resources (e.g., namespace)
    - `postgresql/`: PostgreSQL database resources
    - `vault/`: Vault configuration
    - `keycloak/`: Keycloak configuration
    - `argocd/`: ArgoCD configuration
    - `minio/`: MinIO configuration
    - `secrets/`: Secret templates and management

- `clusters/`: Production cluster configurations
  - `production/`: Production-specific kustomizations

## Component Structure

Each component follows a similar pattern:

```
component/
├── config/                # Application configuration
│   ├── base/
│   └── overlays/
│       ├── local/
│       └── odcn/
└── controller/            # Deployment/controller configuration
    ├── base/
    └── overlays/
        ├── local/
        └── odcn/
```

## Namespace Handling

To avoid namespace duplication, we follow these principles:

1. The namespace is defined once in `bootstrap/infrastructure/common/namespace.yaml`
2. Component configurations reference this common namespace
3. The main kustomization file sets the namespace for all resources

## Local Development

For local development:

1. Use the `cluster-specific-repo/clusters/local-kind-cluster/` kustomization
2. Environment-specific patches are defined in the `patches/` directory
3. Component overrides use the modern patches syntax in the kustomization.yaml file
4. Secret templates are directly included from the templates directory

## Build and Deploy

To build the Kubernetes manifests:

```bash
kustomize build cluster-specific-repo/clusters/local-kind-cluster/ --load-restrictor LoadRestrictionsNone --enable-alpha-plugins
```

This will generate the complete set of Kubernetes resources with all patches applied.