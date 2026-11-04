# ZAD

ZAD (Zelfservice Applicatie Deployment) is the set of tools that together make deploying software for government projects easy, fast and accessible. You describe what your application needs; the platform provisions it, deploys it and keeps it running. It currently runs in ODC-Noord, but is designed to work on any Kubernetes cluster.

One declarative project file describes your deployments and the services they use (PostgreSQL, Keycloak, MinIO, Redis, mail). ZAD provisions those services, generates the credentials and manifests, and ArgoCD rolls them out.

## Using ZAD

There are three ways in, all on the same API:

- **The portal** at [zad.rijksapps.nl](https://zad.rijksapps.nl): create a project, add deployments and services, manage members and backups.
- **[zad-actions](https://github.com/RijksICTGilde/zad-actions)**: GitHub Actions to deploy an image from CI, spin up an environment per pull request and clean it up afterwards.
- **[zad-cli](https://github.com/RijksICTGilde/zad-cli)** (`zadctl`): the same operations from your terminal or any other pipeline.

## Working on the Platform Itself

This repository holds the platform: the Operations Manager, the Kustomize for every component and the tooling around it. To change it, run a full copy on your own machine with the **sandboxed-local** setup. See the **[Getting Started Guide](docs/getting-started.md)** for step-by-step setup instructions.

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
| `odcn-staging` | Staging cluster in ODC-Noord, as a Capsule tenant for pentesting |
| `odcn-production` | Production cluster in ODC-Noord |

## Services

| Service | Purpose |
|---------|---------|
| ArgoCD | GitOps deployment controller |
| Forgejo | In-cluster Git server (sandboxed-local) |
| PostgreSQL | CNPG-managed database cluster |
| Keycloak | Identity and access management, SSO |
| MinIO | S3-compatible object storage |
| SeaweedFS | Fallback object storage |
| Redis | Key-value store for projects |
| Mail relay | Outgoing mail for projects and Keycloak |
| Prometheus | Metrics and monitoring |
| Operations Manager | ZAD self-service portal and API |

## Documentation

- [Getting Started](docs/getting-started.md) - local setup guide
- [Working in the code](docs/development/README.md) - contracts and step-by-step for changing a subsystem; start here before you change code
- [Features](features/README.md) - what each built feature is and how it works, indexed by theme
- [Keycloak Configuration](docs/keycloak/keycloak.md) - Keycloak administration
- [Keycloak YAML Configuration](docs/keycloak/keycloak-yaml-configuration.md) - declarative Keycloak setup
- [Beveiligingsbeleid](SECURITY.md) - hoe je een kwetsbaarheid meldt

### Repository Layout

| Folder | Contains |
|--------|----------|
| `operations-manager/` | The Operations Manager (FastAPI, Python) |
| `infrastructure/`, `bootstrap/` | Kustomize for the platform components and the `rig-system` namespace, per cluster type |
| `images/` | Our own container images (Keycloak with extensions, ArgoCD, PostgreSQL with dictionaries, test workloads) |
| `features/` | One document per built feature |
| `docs/` | `development/` (working in the code), `sandbox/`, `operations/`, `keycloak/`, `security/`, `post-mortems/` |
| `architecture/` | System overviews, decision records, ASVS and BIV assessments |
| `scripts/` | Key rotation, secret scanning and sandbox helpers |
| `weekly/` | Release notes |

Plans, TODO lists, run reports and other outdated material live in the separate repository RIG-Cluster-Legacy, under the same path they had here.

### Feature Documentation

The full list is in [`features/README.md`](features/README.md). Key features:

- [Sandboxed Local Development](features/sandboxed-local-development.md) - sandbox architecture and configuration
- [Sandbox SSO Setup](features/sandbox-sso-setup.md) - connecting to production Keycloak for SSO
- [Backup System](features/backup-system.md) - PVC backup and restore
- [Bootstrap API Actions](features/bootstrap-api-actions.md) - API operations
- [Namespace PostgreSQL Database](features/namespace-postgresql-database.md) - per-project database provisioning
- [CAA Records](features/caa-records.md) - keeps our DNS zones locked to the CA we actually use

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
