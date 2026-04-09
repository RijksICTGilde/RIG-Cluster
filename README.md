# RIG-Cluster

RIG-Cluster is a Kubernetes platform for RIG projects in ODC-Noord, supporting POC, Pilot, and Production environments.

At its core is **ZAD** (Zelfservice Applicatie Deployment) - a self-service portal where developers define what their project needs in a single declarative file. ZAD provisions the infrastructure (PostgreSQL, Keycloak, MinIO, Redis), generates credentials, creates Kubernetes deployments, and configures everything end-to-end. It integrates into CI/CD through a GitHub Action and exposes an API for controlling deployments, updating images, creating backups, spinning up feature branch environments, and cleaning up.

## Getting Started

The recommended way to get started is the **sandboxed-local** setup, which runs a fully self-contained cluster on your machine. See:

- **[Getting Started Guide](docs/getting-started.md)** - step-by-step setup instructions

Quick start:

```bash
# Install required tools
brew install go-task kind kubectl kustomize sops age ksops pwgen jq yq rsync skaffold

# Run the setup (interactive, takes ~5-10 minutes)
task sandbox:setup
```

You will need the developer AGE private key to decrypt the TLS certificates. Ask the ZAD developers if you don't have it.

## Architecture

The platform uses a GitOps approach with ArgoCD. The Operations Manager (ZAD) drives three Git repositories:

1. **zad-projects** - declarative project definitions (one file per project: services, configuration, user accounts, SSO setup)
2. **zad-argo-user-applications** - ArgoCD Application manifests, generated from project definitions
3. **zad-deployments** - Kubernetes manifests (secrets, configmaps, deployments) generated for each project

Project definitions go in, ArgoCD applications and deployment manifests come out, and ArgoCD deploys them to the cluster.

## Cluster Types

| Type | Description |
|------|-------------|
| `sandboxed-local` | Self-contained Kind cluster with in-cluster Forgejo, real TLS (`*.sandbox.rijksapp.dev`) |
| `local` | Kind cluster with external Git (GitHub + git daemon), self-signed CA |
| `odcn-production` | Production cluster in ODC-Noord |

## Services

| Service | Purpose |
|---------|---------|
| ArgoCD | GitOps deployment controller |
| Forgejo | In-cluster Git server (sandboxed-local) |
| PostgreSQL | CNPG-managed database cluster |
| Keycloak | Identity and access management, SSO |
| MinIO | S3-compatible object storage |
| Operations Manager | ZAD self-service portal and API |

## Documentation

- [Getting Started](docs/getting-started.md) - local setup guide
- [Local Kind Cluster Setup](docs/local-kind-cluster-setup.md) - alternative local setup with external Git
- [Keycloak Configuration](docs/keycloak.md) - Keycloak administration
- [Keycloak YAML Configuration](docs/keycloak-yaml-configuration.md) - declarative Keycloak setup

### Feature Documentation

Feature-specific documentation is in the [`features/`](features/) directory. Key features:

- [Sandboxed Local Development](features/sandboxed-local-development.md) - sandbox architecture and configuration
- [Sandbox SSO Setup](features/sandbox-sso-setup.md) - connecting to production Keycloak for SSO
- [Backup System](features/backup-system.md) - PVC backup and restore
- [Bootstrap API Actions](features/bootstrap-api-actions.md) - API operations
- [Namespace PostgreSQL Database](features/namespace-postgresql-database.md) - per-project database provisioning

## Tools

All operations use [Taskfile](https://taskfile.dev). Key commands:

```bash
task sandbox:setup                    # Full sandbox setup
task sandbox:sync                     # Sync infrastructure changes to Forgejo
task sandbox:skaffold-dev             # Hot-reload development
task sandbox:update-operations-manager # Rebuild and deploy Operations Manager
task sandbox:destroy                  # Tear down sandbox
```

## Secret Management

Secrets are managed with SOPS and AGE encryption. Templates in the infrastructure overlays use `@secret-gen:random:XX` annotations for automatic password generation. The sandbox uses a per-setup AGE key (`security/sandbox-key.txt`), while shared secrets (TLS certificates) use a developer AGE key distributed out-of-band.

### APM Setup & Testing 

1. Create a new APM user with their own dedicated APM Server, unique Secret Token, and isolated Kibana space.
    `python3 provision_apm_user.py testprojectname`
   This will deploy a new Kubernetes ApmServer resource named `apm-server-<username>` and provide a unique URL (e.g., `http://testprojectname-apm.sandbox.rijksapp.dev`) and Secret Token.
 
2.
   One-liner to run in an isolated environment:
   ```bash
   python3 -m venv venv && ./venv/bin/pip install elastic-apm && ELASTIC_APM_SERVER_URL=[url, see output from above] ELASTIC_APM_SECRET_TOKEN=[token, see output from above] ./venv/bin/python3 test_apm_lib.py
   ```
   User can then go to https://kibana.sandbox.rijksapp.dev/login and login with their username and password to see their data
