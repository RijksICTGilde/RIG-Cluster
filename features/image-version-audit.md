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

### Redis - 7.4.11-alpine (pinned via RC-353)

- **Current**: `redis:7.4.11-alpine` (pinned October 2026)
- **Latest**: 8.4.1 (`8-alpine`)
- **Files**: `infrastructure/bootstrap/infrastructure/redis/controller/base/deployment.yaml`
- **Impact**: Redis is pinned to the version that ran at the time of RC-353. Updates via Renovate will come as regular PRs.

**Upgrade notes**:
- Redis 7.4+ and 8.x use RSALv2/SSPLv1 license (no longer BSD). Requires a licensing check before upgrading.
- Options: stay on the 7.4 line (support until Nov 2026, same license change) or move to 8.x (tri-license RSALv2/SSPLv1/AGPLv3). Pin a full version either way; the guard rejects `7.4-alpine` and `8-alpine`.
- Redis 7.2 is the last BSD-licensed version.

### Alpine Linux - 3.24.3 (pinned via RC-353)

- **Current**: `alpine:3.24.3` (pinned October 2026; stood on `3.24` before)
- **Latest**: 3.24.3, the newest in the 3.24 line
- **Files**: `images/cmp-kustomize-sops/Dockerfile`, `images/job-test/Dockerfile`, `images/kopia/Dockerfile`, `images/mc/Dockerfile`
- **Impact**: none. `3.24` and `3.24.3` have the same config and layer digest, so nothing changed version. The pin first read `3.24.2`, which `3.24` had already moved past; that one carried CVE-2026-85091 (zlib heap overflow, MEDIUM) and 3.24.3 does not.

**Upgrade notes**:
- Patch and minor updates arrive as regular Renovate PRs; no manual bump needed.
- Alpine supports a minor for two years, so the 3.24 line runs until mid-2028.

### BusyBox - 1.38.0 (pinned via RC-353)

- **Current**: `busybox:1.38.0` (pinned October 2026; stood on `1.38` before)
- **Latest**: may be newer in the 1.38 line
- **Files**: `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml`, `infrastructure/bootstrap/infrastructure/forgejo/controller/overlays/sandboxed-local/statefulset-patch.yaml`
- **Impact**: none. `1.38` and `1.38.0` were digest-identical when pinned.

**Upgrade notes**:
- Updates arrive as regular Renovate PRs.
- Both references are init containers that only chown and chmod a volume, so a bump has a small surface.

## Medium Priority

### Keycloak - 25.0.6

- **Current**: `quay.io/keycloak/keycloak:25.0.6`, the base of our own image
- **Latest**: 26.5.3
- **Files**: `images/keycloak-zad/Dockerfile` (base version); the deployment pins an image tag

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
- **Files**: `bootstrap/sandbox/kind-config.yaml`

**Upgrade notes**:
- K8s 1.32 nearing end-of-life (~Feb 2026). 11 patch releases behind at minimum.
- Minimum: update to `kindest/node:v1.32.11`.
- Full upgrade to v1.35.0 requires Kind v0.31.0. Note that K8s 1.35 drops cgroup v1 support.

### Chisel - 1.12.0

The chisel server manifest under `infrastructure/bootstrap/infrastructure/chisel/` stood on
`:latest`. No cluster kustomization includes it, so nothing deploys it, but RC-353 pinned it
anyway: `1.12.0` is digest-identical to what `latest` resolved to, and it matches the client
version in the OPI image. Whether the manifest stays at all is still a human's call; the pin
does not settle that.

**Upgrade notes**:
- The chisel CLIENT ships in the OPI image (`operations-manager/Dockerfile`, `ARG CHISEL_VERSION`) and is used by `opi/connectors/chisel_connector.py` for tunneled database and bucket clones. Both now name 1.12.0, so move them together.
- Latest upstream is `1.13.0`. Renovate reads the manifest (the `kubernetes` manager covers `infrastructure/**`), so that update arrives as a normal PR.
- Deploying the server? Add the directory to a cluster kustomization. `infrastructure/bootstrap/infrastructure/secrets/templates/chisel-auth-secret.yaml` holds the matching auth-secret template.

## Low Priority

### PgAdmin - 9.8.0

- **Current**: `dpage/pgadmin4:9.8.0`
- **Latest**: 9.12 (released 2026-02-05)
- **Files**: `infrastructure/bootstrap/infrastructure/pgadmin/controller/base/deployment.yaml`
- Straightforward minor upgrade within the same v9.x line.

### Forgejo - 14.0.5 (pinned via RC-353)

- **Current**: `codeberg.org/forgejo/forgejo:14.0.5` / `:14.0.5-rootless`
- **Latest**: May be newer in the 14.x line
- **Files**: `infrastructure/bootstrap/infrastructure/forgejo/controller/base/statefulset.yaml`, `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml`, sandbox overlays
- Pinned to 14.0.5 via RC-353 (October 2026). Updates via Renovate will come as regular PRs.

### Python - 3.14-slim

- **Current**: `python:3.14-slim`
- **Latest**: 3.14.3 (released 2026-02-03)
- **Files**: `operations-manager/Dockerfile`, `operations-manager/backup-image/Dockerfile`
- 3.13 still supported until ~October 2029; project moved to 3.14 for PEP 649 deferred annotation evaluation.

### CloudNativePG PostgreSQL - 17.11 standard-bookworm (pinned via RC-353)

- **Current**: `ghcr.io/cloudnative-pg/postgresql:17.11-202610260819-standard-bookworm@sha256:fef8075b...` (pinned October 2026; stood on `17-standard-bookworm` before)
- **Latest**: PostgreSQL 18.x available
- **Files**: `images/postgresql-with-dictionaries/Dockerfile`
- **Impact**: none. The pinned tag was digest-identical to what `17-standard-bookworm` resolved to.
- The platform database itself (`infrastructure/bootstrap/infrastructure/postgresql/database/`, base plus the two ODCN overlays) stands on `ghcr.io/cloudnative-pg/postgresql:17.11`, digest-identical to the `:17` it stood on (`sha256:70664ebc...`). CNPG numbers that image after the PostgreSQL release, so two digit groups is the full version there; `TestVasteVersies` carries it as a named exception. Rolling the pin replaces instance pods: CNPG's `checkPodImageIsOutdated` compares images as strings, not digests. Expect a switchover on the two-instance ODCN cluster, even though the content is identical.

**Upgrade notes**:
- CNPG publishes no patch digit in its tag, but it does publish a tag carrying the build timestamp, and that one never moves. The `packageRule` for this image in `renovate.json` uses a `versioning: "regex"` so updates stay inside the standard-bookworm line and no proposal jumps to bullseye or trixie.
- PG 17 is supported until ~November 2029. Plan the PG 18 migration at convenience; that is a database migration, not an image bump.

## Suggested Upgrade Order

1. ~~**Alpine** and **BusyBox**~~ - pinned in RC-353 (October 2026); updates now arrive via Renovate
2. ~~**Chisel :latest**~~ - pinned to 1.12.0 in RC-353; whether the manifest stays is still a human's call
3. **Redis 7.4.11-alpine -> 8-alpine** - EOL imminent, but check license implications
4. **MinIO** - security CVE, but investigate UI removal and build-from-source requirement first
5. **PgAdmin, Kube State Metrics, External DNS** - straightforward minor upgrades
6. **Keycloak 25.0.1 -> 26.5.x** - review migration guide, moderate effort
7. **Prometheus v2 -> v3** - major upgrade, plan and test
8. **ArgoCD v2.8 -> v3.x + Operator v0.14 -> v0.17** - largest effort, coordinate both, incremental path required
9. **Kind node, Python, PostgreSQL** - upgrade at convenience

## Floating tags

No third-party image under `infrastructure/`, `bootstrap/`, `images/` or `scripts/` stands
on a tag that moves anymore. `TestVasteVersies` in
`operations-manager/python/tests/test_image_layer_guard.py` holds that, and names the reason
per remaining exception.
