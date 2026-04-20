# Container Image Version Audit

Audit of all container images used across the repository, performed February 2026. This document tracks which images are outdated and serves as a reference for planning upgrades.

## Critical / High Priority

### ArgoCD - v2.8.4 (EOL)

- **Current**: `quay.io/argoproj/argocd:v2.8.4`
- **Latest**: v3.3.0 (released 2026-02-02)
- **Files**: `images/cmp-kustomize-sops/Dockerfile`
- **Impact**: v2.8 is well past end-of-life. Only v2.14 still receives patches in the 2.x line. No security fixes.

**Upgrade notes**:
- Major version bump with breaking changes: RBAC (update/delete no longer apply to sub-resources), logs RBAC enforced by default, annotation-based tracking by default, legacy repo config in `argocd-cm` removed, Helm upgraded to 3.17.1.
- Requires incremental upgrade path: 2.8 -> 2.9 -> ... -> 2.14 -> 3.0 -> 3.1 -> 3.2 -> 3.3.
- The ArgoCD Operator must be upgraded in coordination (v0.17.0 depends on ArgoCD v3.1.x).

### ArgoCD Operator - v0.14.0 (consider dropping entirely)

- **Current**: `quay.io/argoprojlabs/argocd-operator:v0.14.0`
- **Latest**: v0.17.0 (released 2026-01-19)
- **Files**: `bootstrap/crd/operator/argocd-operator-install.yaml`
- **Impact**: Must be upgraded together with ArgoCD. v0.17.0 internally depends on ArgoCD v3.1.x.

**Consider migrating from operator to Helm chart (`argo/argo-cd`)**:
- The operator adds an abstraction layer with limited benefit for a single-instance setup with heavy customization (CMP sidecar, custom env vars, volumes).
- Known issue: operator creates deployments before fully reconciling sidecar/volume config, requiring a restart workaround in `bootstrap-argo-system`.
- Upgrade coupling: ArgoCD version is tied to operator version, making the v3.x upgrade a two-component effort instead of one.
- The Helm chart gives direct control, CMP sidecars are first-class config, and ArgoCD upgrades are just a version bump.
- **Action needed**: Investigate what ODC-N provides in the production cluster. They currently provide the ArgoCD operator/CR - determine whether we are required to use their operator or can manage our own Helm-based ArgoCD install within our namespace. Even if they provide the operator, a self-managed Helm install may be possible and preferable.

### MinIO - Security CVE

- **Current**: `minio/minio:RELEASE.2025-01-20T14-49-07Z` (backup-destination) and `RELEASE.2025-07-23T15-54-02Z` (infrastructure)
- **Latest**: RELEASE.2025-10-15T17-29-55Z
- **Files**: `bootstrap/rig-system/kustomize/backup-destination/base/deployment.yaml`, `infrastructure/bootstrap/infrastructure/minio/controller/base/deployment.yaml`, `infrastructure/bootstrap/infrastructure/minio/controller/base/deployment-versioned.yaml`, `infrastructure/bootstrap/infrastructure/backup-destination/controller/base/deployment.yaml`
- **Impact**: Privilege escalation CVE (GHSA-jjjj-jwhf-8rgr).

**Upgrade notes**:
- Embedded UI Console has been removed (May 2025+). External IDP logins via LDAP/OIDC in the console also removed (STS APIs still work).
- Community edition is now distributed as source code only - no pre-built binaries. Container images need to be built from source or via Helm chart.
- Investigate what this means for the current deployment before upgrading.

### Redis - 7.2 EOL (February 28, 2026)

- **Current**: `redis:7-alpine` (resolves to 7.2.x)
- **Latest**: 8.4.1 (`8-alpine`)
- **Files**: `infrastructure/bootstrap/infrastructure/redis/controller/base/deployment.yaml`
- **Impact**: Redis 7.2 security support ends February 28, 2026.

**Upgrade notes**:
- Redis 7.4+ and 8.x use RSALv2/SSPLv1 license (no longer BSD). Requires a licensing check before upgrading.
- Options: `7.4-alpine` (support until Nov 2026, same license change) or `8-alpine` (latest, tri-license RSALv2/SSPLv1/AGPLv3).
- Redis 7.2 is the last BSD-licensed version.

### Alpine Linux - 3.19 (unsupported)

- **Current**: `alpine:3.19`
- **Latest**: 3.23.3 (released 2026-01-28)
- **Files**: `images/cmp-kustomize-sops/Dockerfile`, `images/kopia/Dockerfile`
- **Impact**: 3.19 community repository support has ended. No security patches.

**Upgrade notes**:
- Straightforward upgrade. Use `3.21` (conservative) or `3.23` (latest).
- Quick win, low risk.

### BusyBox - 1.28 (extremely outdated)

- **Current**: `1.28` (Taskfile), `1.36` (bootstrap-job, tests), `1.37.0` (Keycloak init)
- **Latest**: 1.37.0
- **Files**: `Taskfile.yaml` (line 556), `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml`, `operations-manager/python/tests/fixtures/test-workloads.yaml`, `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml`
- **Impact**: `1.28` is from ~2018.

**Upgrade notes**:
- Standardize all BusyBox references to `1.37.0`.
- Quick win, low risk.

## Medium Priority

### Keycloak - 25.0.1

- **Current**: `quay.io/keycloak/keycloak:25.0.1`
- **Latest**: 26.5.3
- **Files**: `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml`

**Upgrade notes**:
- Not a traditional major version break (continuous release model since Quarkus migration).
- Security CVEs fixed in 26.5.x (CVE-2026-1609, CVE-2026-1529, CVE-2026-1486).
- Key changes: JWT Authorization Grants, session_state/sid format change (UUID to base64), client session timeout validation, message keys no longer support HTML in login theme.
- Review the [Keycloak migration guide](https://www.keycloak.org/docs/latest/upgrading/) per minor version from 25.0 to 26.5.

### Prometheus - v2.54.1

- **Current**: `prom/prometheus:v2.54.1`
- **Latest**: v3.9.1 (LTS: v3.5.1)
- **Files**: `infrastructure/bootstrap/infrastructure/prometheus/controller/base/deployment.yaml`

**Upgrade notes**:
- Major version bump. New UI, native histograms promoted, Agent mode stable.
- LTS release v3.5.1 available as a more conservative option.
- Review the [Prometheus 3.0 migration guide](https://github.com/prometheus/prometheus/blob/main/CHANGELOG.md).

### Kube State Metrics - v2.13.0

- **Current**: `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0`
- **Latest**: v2.18.0 (released 2026-01-11)
- **Files**: `infrastructure/bootstrap/infrastructure/prometheus/controller/base/kube-state-metrics-deployment.yaml`

**Upgrade notes**:
- 5 minor versions behind.
- v2.18.0: `endpointslices` now default (replaces `endpoints`, which is deprecated). If relying on endpoint metrics, manually activate via `--resources` flag.
- New metrics added: `kube_job_status_ready`, `kube_deployment_owner`, `kube_deployment_status_replicas_terminating`.

### External DNS - v0.15.0

- **Current**: `rcr.rijksapps.nl/k8s-rig/external-dns/external-dns:v0.15.0`
- **Latest**: v0.20.0 (released 2025-11-14)
- **Files**: `infrastructure/bootstrap/infrastructure/external-dns/controller/base/deployment.yaml`

**Upgrade notes**:
- 5 minor versions behind. CLI migrated from kingpin to cobra.
- Note: `--min-ttl` was unintentionally removed in v0.20.0 and is expected back in v0.21.0. If depending on `--min-ttl`, wait for v0.21.0.
- Uses a private registry mirror - the mirror must be updated as well.

### Kind Node - v1.32.0

- **Current**: `kindest/node:v1.32.0`
- **Latest**: v1.32.11 (same K8s version, patched) / v1.35.0 (latest Kind default)
- **Files**: `kind-config.yaml`, `sandboxed-local/kind-config.yaml`

**Upgrade notes**:
- K8s 1.32 nearing end-of-life (~Feb 2026). 11 patch releases behind at minimum.
- Minimum: update to `kindest/node:v1.32.11`.
- Full upgrade to v1.35.0 requires Kind v0.31.0. Note that K8s 1.35 drops cgroup v1 support.

### Chisel - :latest (unpinned)

- **Current**: `jpillora/chisel:latest`
- **Latest**: v1.11.3 (released Sep 2025)
- **Files**: `infrastructure/bootstrap/infrastructure/chisel/controller/base/deployment.yaml`

**Upgrade notes**:
- Pin to `jpillora/chisel:1.11.3` for reproducibility.

## Low Priority

### PgAdmin - 9.8.0

- **Current**: `dpage/pgadmin4:9.8.0`
- **Latest**: 9.12 (released 2026-02-05)
- **Files**: `infrastructure/bootstrap/infrastructure/pgadmin/controller/base/deployment.yaml`
- Straightforward minor upgrade within the same v9.x line.

### Forgejo - :14 / :14-rootless

- **Current**: `codeberg.org/forgejo/forgejo:14` / `:14-rootless`
- **Latest**: 14.0.2 (released 2026-01-29)
- **Files**: `infrastructure/bootstrap/infrastructure/forgejo/controller/base/statefulset.yaml`, `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml`, sandbox overlays
- Already current - floating `:14` tag resolves to latest 14.0.x.

### Python - 3.14-slim

- **Current**: `python:3.14-slim`
- **Latest**: 3.14.3 (released 2026-02-03)
- **Files**: `operations-manager/Dockerfile`, `operations-manager/backup-image/Dockerfile`
- 3.13 still supported until ~October 2029; project moved to 3.14 for PEP 649 deferred annotation evaluation.

### CloudNativePG PostgreSQL - :17

- **Current**: `ghcr.io/cloudnative-pg/postgresql:17`
- **Latest**: PostgreSQL 18.1 available
- **Files**: `images/postgresql-with-dictionaries/Dockerfile`
- `:17` resolves to latest 17.x (17.7). PG 17 is supported until ~November 2029. Plan PG 18 migration at convenience.

## Suggested Upgrade Order

1. **Alpine 3.19 -> 3.21+** and **BusyBox 1.28 -> 1.37.0** - quick wins, no breaking changes
2. **Chisel :latest -> 1.11.3** - pin for reproducibility
3. **Redis 7-alpine -> 7.4-alpine or 8-alpine** - EOL imminent, but check license implications
4. **MinIO** - security CVE, but investigate UI removal and build-from-source requirement first
5. **PgAdmin, Kube State Metrics, External DNS** - straightforward minor upgrades
6. **Keycloak 25.0.1 -> 26.5.x** - review migration guide, moderate effort
7. **Prometheus v2 -> v3** - major upgrade, plan and test
8. **ArgoCD v2.8 -> v3.x + Operator v0.14 -> v0.17** - largest effort, coordinate both, incremental path required
9. **Kind node, Python, PostgreSQL** - upgrade at convenience
