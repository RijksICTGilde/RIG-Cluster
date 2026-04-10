# Plan: BIO2 Security Pipeline - Docker CI/CD, Vulnerability Scanning, Renovate

## Summary

Three capabilities for BIO2 compliance (A8.08 vulnerability management):

1. **Docker build & publish** - CalVer + SHA hybrid versioning
2. **Vulnerability scanning** - pip-audit + Trivy + SBOM generation
3. **Renovate replaces Dependabot** - unified dependency management including infra manifests

## Implementation

### 1. `.github/workflows/docker.yml` - ZAD (Operations Manager)

**Automated CI pipeline for the main application image.**

- **Triggers**: Push to main + PRs (path-filtered to `operations-manager/`)
- **Main**: CalVer tag (`YYYY.M.PATCH`, e.g., `2026.3.1`) + `latest` + `sha-xxx`
- **PRs**: `pr-{number}-sha-{short}` + `sha-xxx`
- **Registry**: `ghcr.io/minbzk/base-images/operations-manager`
- **Platforms**: linux/amd64, linux/arm64
- **Cache**: GitHub Actions cache (`type=gha`)
- CalVer git tag created automatically on main

### 2. `.github/workflows/docker-images.yml` - Supporting Images

**Manual trigger only (`workflow_dispatch`) for other images.**

- **Images**: rig-backup, cmp-kustomize-sops, postgresql-with-dictionaries
- **Input**: choose one image or "all"
- **Tagging**: CalVer + `sha-xxx` + `latest`
- Shared CalVer calculation in `prepare` job

### 3. `.github/workflows/security.yml` - Vulnerability Scanning

- **Triggers**: Push to main, PRs, weekly schedule (Monday 6am UTC)
- **Jobs**:
  - `dependency-audit`: pip-audit --strict on Python deps
  - `container-scan`: Trivy HIGH/CRITICAL scan on all 4 images (matrix)
  - `sbom`: CycloneDX SBOM generation (main only), uploaded as artifact
- SARIF results uploaded to GitHub Security tab

### 4. `renovate.json` - Replaces Dependabot

- `.github/dependabot.yml` deleted
- Renovate covers: Python (uv), GitHub Actions, Dockerfiles, **Kubernetes manifests** (infra + bootstrap)
- Weekly schedule, grouped minor+patch updates
- Requires Renovate GitHub App installation on the repo (manual step)

### 5. `pyproject.toml` - pip-audit added to dev dependencies

### 6. `Taskfile.yaml` - Local scanning tasks

- `task security-audit` - runs pip-audit locally
- `task security-scan-image` - builds and scans with Trivy locally

## Manual Steps After Merge

1. Install [Renovate GitHub App](https://github.com/apps/renovate) on the repo
2. Verify GHCR packages write permission for GitHub Actions (`Settings > Actions > General`)
3. Check GitHub Security tab after first security.yml run
