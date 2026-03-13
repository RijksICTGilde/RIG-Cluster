# Operations Manager (OPI)

The Operations Manager is the core application of ZAD (Zelfservice Applicatie Deployment). It is a FastAPI application deployed per Kubernetes cluster that:

1. Provides a web UI for creating and managing projects through forms and wizards
2. Exposes a REST API for CI/CD integration (image updates, backups, feature branches)
3. Provisions infrastructure services per project (PostgreSQL, Keycloak, MinIO, Redis)
4. Generates Kubernetes manifests from Jinja2 templates and commits them to Git
5. Manages ArgoCD applications for GitOps deployment

## Quick Start (Development)

```bash
cd operations-manager/python
uv sync
uv run pytest tests/ -q
```

For hot-reload in the sandbox cluster:

```bash
task sandbox:skaffold-dev    # API available at localhost:9595
```

For a full rebuild and deploy:

```bash
task sandbox:update-operations-manager
```

## Deployment

See [deploy.md](deploy.md) for building and publishing the Docker image.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the full module map and design patterns, or [architecture/operations-manager.md](../architecture/operations-manager.md) for diagrams.
