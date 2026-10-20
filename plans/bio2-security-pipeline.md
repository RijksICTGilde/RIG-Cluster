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
  - `container-scan`: Trivy HIGH/CRITICAL scan on all 6 images (matrix)
    - The gate scans twice: once for the SARIF upload to the GitHub Security tab, then with `exit-code: 1` to fail on solvable findings
    - Solvable findings are suppressed via `.trivyignore.yaml` (each entry requires a reason and expiry date)
    - Matrix includes 5 images built by CI plus argocd-rig (scanned as a non-built image using the production pin)
  - `sbom`: CycloneDX SBOM generation (main only), uploaded as artifact
- SARIF results uploaded to GitHub Security tab regardless of gate outcome

### 4. `renovate.json` - Replaces Dependabot

- `.github/dependabot.yml` deleted
- Renovate covers: Python (uv), GitHub Actions, Dockerfiles, **Kubernetes manifests** (infra + bootstrap)
- Weekly schedule, grouped minor+patch updates
- Requires Renovate GitHub App installation on the repo (manual step)

### 5. `.trivyignore.yaml` - Vulnerability Exceptions

- Suppresses solvable findings (no upstream fix available)
- Each entry requires: CVE/vulnerability ID, reason (`statement`), and expiry date (`expired_at`, max 90 days)
- OS-level exceptions scoped to package names via `purls` to prevent overly broad suppression
- Test: `tests/test_trivyignore.py` enforces non-empty statement/expiry and validates expiry is within bounds

### 6. `pyproject.toml` - pip-audit added to dev dependencies

### 7. `Taskfile.yaml` - Local scanning tasks

- `task security-audit` - runs pip-audit locally
- `task security-scan-image` - builds and scans with Trivy locally

## Manual Steps After Merge

1. Install [Renovate GitHub App](https://github.com/apps/renovate) on the repo
2. Verify GHCR packages write permission for GitHub Actions (`Settings > Actions > General`)
3. Check GitHub Security tab after first security.yml run
4. Monitor first security.yml runs on new PRs: if the `container-scan` gate fails, review findings in the workflow and populate `.trivyignore.yaml` with reasons and expiry dates for solvable findings (or fix them via Renovate/dependency bumps)
