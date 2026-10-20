# Security Scanning Pipeline

BIO2 A8.08 vulnerability management implementation.

## Overview

Automated security scanning in GitHub Actions covering:

- **Python dependency vulnerabilities** via pip-audit
- **Container image vulnerabilities** via Trivy
- **SBOM generation** for supply chain transparency
- **Dependency updates** via Renovate (zie `features/renovate.md`)

## Docker Image CI/CD

### Operations Manager (ZAD)

Automated build on push to main and PRs.

- **Workflow**: `.github/workflows/docker.yml`
- **Registry**: `ghcr.io/rijksictgilde/zad/operations-manager`
- **Versioning**: CalVer (`YYYY.M.PATCH`) on main, `pr-N-sha-X` on PRs
- **Platforms**: linux/amd64, linux/arm64

### Supporting Images

Manual trigger via GitHub Actions UI.

- **Workflow**: `.github/workflows/docker-images.yml`
- **Images**: rig-backup, cmp-kustomize-sops, postgresql-with-dictionaries, keycloak-zad
- **Versioning**: CalVer on dispatch

## Security Scanning

**Workflow**: `.github/workflows/security.yml`

| Job | Tool | What | When |
|-----|------|------|------|
| `dependency-audit` | pip-audit | Python deps against PyPI/OSV | Push, PR, weekly |
| `secret-scan` | `scripts/scan-secrets.py` | Every tracked file in the branch tree | Push, PR, weekly |
| `secret-scan-history` | `scripts/scan-secrets.py --history` | Every blob that ever existed, minus `scripts/secret-scan-baseline.json` | Weekly and on demand |
| `container-scan` | Trivy | Every image CI publishes (HIGH/CRITICAL) | Push, PR, weekly |
| `sbom` | Trivy | CycloneDX SBOM | Main only |

Results appear in the **GitHub Security tab** (SARIF upload). The secret scan is the exception: its
output stays in the job log and goes nowhere else, because a list of where secrets sit is a map to
what is not revoked yet. The same tree scan also runs as `test_this_repository_is_clean_right_now`
in `tests/test_secret_scan.py`, so a build outside GitHub has the guard too. The layers and the
baseline are in `features/sops-sleutel-roteren.md`.

## Dependency Management

**Renovate** draait als GitHub Action (`.github/workflows/renovate.yml`) met de
configuratie in `renovate.json`, en vervangt Dependabot. Elke maandagochtend maakt het
PR's per ecosysteem voor Python, Go, Maven, npm, Dockerfiles, Kubernetes-manifesten,
remote kustomize-resources, de Taskfile, `.tool-versions` en de GitHub Actions zelf.
Majors komen alleen op het Dependency Dashboard en worden pas een PR na goedkeuring.

Zie `features/renovate.md` voor de dekking, de rcr-mapping, wat met opzet niet wordt
bijgewerkt, en de handstap bij het inrichten.

## Setup

### TODO

- [ ] Zet het repository secret `RENOVATE_TOKEN` en start `renovate.yml` een keer met
      `workflow_dispatch` (zie `features/renovate.md`)
- [ ] Verify GHCR packages write permission for GitHub Actions (`Settings > Actions > General > Workflow permissions`)
- [ ] Loop de eerste ronde Renovate-PR's na
- [ ] Check GitHub Security tab after first `security.yml` run for SARIF results
- [ ] Migrate Taskfile publish tasks from `ghcr.io/minbzk/base-images/` to `ghcr.io/rijksictgilde/zad/`
- [ ] Update image references in `bootstrap/` deployment patches (operations-manager, cmp-kustomize-sops)
- [ ] Update image references in `infrastructure/` manifests (rig-backup, etc.)
- [ ] Update Skaffold configs (`operations-manager/skaffold*.yaml`) to new registry path

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
