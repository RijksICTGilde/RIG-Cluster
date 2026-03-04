=========================================
 ZAD Sandbox - Setup Information
=========================================

WHAT IS ZAD?

  ZAD (Zelfservice Applicatie Deployment) is a self-service portal where
  developers set up complex workloads through a clean UI, without dealing
  with infrastructure. It automatically provisions services (PostgreSQL,
  Keycloak, MinIO, Redis), creates credentials, generates Kubernetes
  deployments, and configures everything end-to-end.

  Developers focus on code - ZAD handles the platform. It also includes
  backup and cloning capabilities for feature branch deployments.

  ZAD integrates into CI/CD through a ready-made GitHub Action, and
  its API offers numerous options — with more being added — to control
  deployments, update images, create backups, spin up feature branch
  environments, and clean up when done.

HOW IT WORKS (GitOps)

  The Operations Manager (ZAD) uses three Git repositories in Forgejo:

  1. zad-projects
     One file per project — the complete playbook for your
     application. It declares which services to provision, how to
     configure them (SSO with Keycloak, local user accounts,
     database schemas, object storage buckets), and how to deploy
     your application. Everything from infrastructure to user-facing
     settings in a single, declarative definition. Define what your
     project needs — ZAD builds the rest.

  2. zad-argo-user-applications
     ArgoCD Application manifests, generated from the project definitions.

  3. zad-deployments
     Kubernetes manifests (secrets, configmaps, deployments) generated
     for each project.

  Together, these three repos drive the GitOps workflow: project
  definitions go in, ArgoCD applications and deployment manifests
  come out, and ArgoCD deploys them to the cluster.

WHAT WILL BE INSTALLED

  A Kind cluster running entirely on your machine with:
  - ArgoCD          (GitOps deployment controller)
  - Forgejo         (in-cluster Git server)
  - PostgreSQL      (CNPG-managed database cluster)
  - Keycloak        (identity and access management)
  - MinIO           (S3-compatible object storage)
  - Operations Manager (the ZAD self-service portal)

PORT REQUIREMENTS

  The Kind cluster binds to ports 80 and 443 on your machine to
  route web traffic directly, without needing a load balancer or
  extra routing tools. Make sure nothing else is using these ports
  before starting setup.

REQUIRED TOOLS

  Install with brew:
    brew install kind kubectl kustomize sops age pwgen yq

  Also required (not in brew):
    - Docker Desktop (must be running)
    - Skaffold (for hot-reload development, optional for setup)

REQUIRED FILES

  Developer AGE private key (ask the ZAD developers).
  You will be prompted to paste it during setup. It starts with
  AGE-SECRET-KEY-... and will be saved locally for future use.

  The TLS wildcard certificates for *.sandbox.rijksapp.dev are
  stored AGE-encrypted in the repository and will be decrypted
  automatically during setup using this key.

ESTIMATED TIME

  First-time setup: ~5-10 minutes (depends on download speeds)

NO EXTERNAL IMPACT

  This runs entirely on your local machine. No production systems
  are accessed or modified. The cluster is isolated in Docker via Kind.

=========================================
