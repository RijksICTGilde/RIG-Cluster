# Security Scanning Pipeline

BIO2 A8.08 vulnerability management implementation.

## Overview

Automated security scanning in GitHub Actions covering:

- **Python dependency vulnerabilities** via pip-audit
- **Container image vulnerabilities** via Trivy
- **SBOM generation** for supply chain transparency
- **Dependency updates** via Renovate (replaces Dependabot)

## Docker Image CI/CD

### Operations Manager (ZAD)

Automated build on push to main and PRs.

- **Workflow**: `.github/workflows/docker.yml`
- **Registry**: `ghcr.io/minbzk/base-images/operations-manager`
- **Versioning**: CalVer (`YYYY.M.PATCH`) on main, `pr-N-sha-X` on PRs
- **Platforms**: linux/amd64, linux/arm64

### Supporting Images

Manual trigger via GitHub Actions UI.

- **Workflow**: `.github/workflows/docker-images.yml`
- **Images**: rig-backup, cmp-kustomize-sops, postgresql-with-dictionaries
- **Versioning**: CalVer on dispatch

## Security Scanning

**Workflow**: `.github/workflows/security.yml`

| Job | Tool | What | When |
|-----|------|------|------|
| `dependency-audit` | pip-audit | Python deps against PyPI/OSV | Push, PR, weekly |
| `container-scan` | Trivy | All 4 Docker images (HIGH/CRITICAL) | Push, PR, weekly |
| `sbom` | Trivy | CycloneDX SBOM | Main only |

Results appear in the **GitHub Security tab** (SARIF upload).

## Dependency Management

**Renovate** (`renovate.json`) replaces Dependabot. It manages:

- Python packages (uv/pyproject.toml)
- GitHub Actions versions
- Dockerfile base images and tool versions
- Container image tags in Kubernetes manifests (`infrastructure/`, `bootstrap/`)
- Helm chart versions

Schedule: weekly, Monday before 9am.

## Local Usage

```bash
# Check Python dependencies for vulnerabilities
task security-audit

# Scan Docker image with Trivy (requires Trivy installed)
task security-scan-image
```

Or directly:

```bash
cd operations-manager/python
uv run pip-audit --strict --desc
```
