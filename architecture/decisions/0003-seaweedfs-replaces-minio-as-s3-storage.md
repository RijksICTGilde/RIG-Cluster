# 0003 - SeaweedFS replaces MinIO as S3 storage

**Status**: Accepted
**Date**: 2026-10-22

## Context

MinIO, the object storage system used by ZAD projects, is archived upstream: no new releases, no security updates, and its images are no longer distributed. The platform builds MinIO from a frozen source version and maintains its own `mc` client in a private mirror. That maintenance burden only grows, and security vulnerabilities accumulate. We need a successor that:

1. Preserves all existing functionality for applications using object storage
2. Requires minimal migration effort per project
3. Is actively maintained upstream

## Decision

SeaweedFS (Apache 2.0, actively maintained) is the S3 storage of ZAD and the successor of MinIO. The service is `seaweedfs-storage`.

- **Gate first (RC-331).** Before committing, a proef on the sandbox verified that SeaweedFS covers every MinIO capability the platform relies on: 34 functional requirements across usage, isolation, backup, clone and metrics, recorded in the gate table (the "eisentabel") in `features/seaweedfs-objectopslag.md`. All 34 pass ("werkt", or "werkt anders" on a non-breaking path).
- **On every cluster (RC-381).** The service runs on `sandboxed-local`, `odcn-staging` and `odcn-production`, with backup, restore, clone, deferred cleanup and the metrics scrape job wired.
- **MinIO is replaced (RC-382).** `minio-storage` carries `replaced_by: seaweedfs-storage`. A new project no longer sees MinIO; a project that already has MinIO keeps it until it is migrated.

## Consequences

**Positive:**
- An S3 store with active upstream maintenance, no longer a frozen build we patch ourselves
- Applications see identical `OBJECT_STORE_*` environment variables and S3 API: switching is a change of endpoint
- Backup and restore share the existing bucket path with MinIO (`backup_label` `minio`)

**Negative:**
- The OPI image gains 212 MiB (the `weed` binary)
- Administration goes through `weed shell` (gRPC) instead of the S3 API with `mc admin` commands, so the filer and master gRPC ports are open to the OPI pod
- SeaweedFS provides bucket size metrics but not per-bucket object counts
- No web admin UI (MinIO console); administration is CLI-only
- A restore or clone writes into the same bucket; there are no bucket generations (`..._vN`) as with MinIO
- Bucket version history is not carried by `mc mirror`-style copies, in a clone or in a migration

**Open:**
- Migration of existing MinIO data: the copy itself is measured, but carrying all object versions, a per-object checksum gate and a plan for applications that store version ids are not built. See "Wat een migratie van bestaande MinIO-data zou vragen" in `features/seaweedfs-objectopslag.md`.
- Removing MinIO from the clusters once no project uses it.

## Alternatives Considered

We were looking for a drop-in replacement for one MinIO instance: a relatively simple S3 server that runs as an ordinary workload on a PVC, not a storage platform. The rows marked "not measured" are judged on documentation, not tested in the proef.

| Alternative | Why not |
|---|---|
| Ceph (Rook, RGW) | A full storage platform (block, file, object) with its own operator, MONs and OSDs. Not a drop-in for one MinIO pod, the operator needs cluster-wide rights we lack as a tenant on ODCN, and production already runs on Ceph underneath (`ocs-storagecluster-ceph-rbd`). Total bucket data was 4.1 GiB on 2026-08-28. |
| Object storage from ODCN's OCS (RGW/NooBaa via ObjectBucketClaims) | Nothing to run ourselves, but not available in the Kind sandbox, and bucket isolation, access keys and metrics would sit with ODCN. Not measured. |
| Garage | Lightweight and built for small clusters, but as far as we know it has no bucket versioning, which the platform offers per bucket. Not measured. |
| RustFS | Positions itself as a MinIO replacement, but is a young project. Not measured. |

## See Also

- `features/seaweedfs-objectopslag.md` - full feature description, gate table (34 rows, all measured), limitations and migration notes
- `features/dienst-vervangen-door-een-opvolger.md` - how `replaced_by` hides MinIO from new projects
- `features/impliciete-dienstselectie.md` - why the service can auto-enroll on deployment
- `docs/KNOWN-ISSUES.md` - why MinIO is an interim measure
