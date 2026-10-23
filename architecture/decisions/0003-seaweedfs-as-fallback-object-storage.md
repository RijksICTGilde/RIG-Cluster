# 0003 - SeaweedFS as fallback object storage when MinIO maintenance burden grows

**Status**: Accepted
**Date**: 2026-10-22

## Context

MinIO, the current object storage system used by ZAD projects, is archived upstream (no new releases, no security updates). The platform builds MinIO from a frozen source version and maintains its own `mc` client in a private mirror. As this maintenance burden grows and security vulnerabilities accumulate, we need a fallback that:

1. Preserves all existing functionality for applications using object storage
2. Requires minimal migration effort per project
3. Is actively maintained upstream

## Decision

Introduce SeaweedFS (Apache 2.0, actively maintained) as a second object-storage service alongside MinIO, deployable on sandbox environments. This is a **proef (proof)** in RC-331:

- Verify SeaweedFS can replicate all MinIO capabilities required by the platform (34 functional requirements across usage, isolation, backup, clone, and metrics)
- Build the service connector, manager, and OPI integration on sandbox only
- Defer production deployment and migration strategy to a follow-up task

The decision is captured in an acceptance gate (the "eisentabel" in `features/seaweedfs-objectopslag.md`): all 34 requirements are measured, and all pass ("werkt" or "werkt anders" on a non-breaking path).

## Consequences

**Positive:**
- A proven alternative to MinIO with active upstream maintenance
- Applications see identical `OBJECT_STORE_*` environment variables and S3 API
- No backup/clone/migration work yet—those roads exist but are not wired, keeping scope tight
- Metrics for bucket size are available; object count per bucket is not (acceptable limitation)

**Negative:**
- The OPI image gains 212 MiB (the `weed` binary)
- The S3-gateway of SeaweedFS (`weed server`) differs from MinIO's architecture: administration goes through `weed shell` (gRPC) instead of the S3 API with `mc admin` commands
- Bucket version history is not included in `mc mirror`-style copies, so a full-versioning migration would require per-version copies (not measured in the proef)
- SeaweedFS provides bucket size metrics but not per-bucket object counts
- Web admin UI (MinIO console) is not available; administration is CLI-only

**For future scopes:**
- Backup and restore: measured in the gate, not wired yet (no `backup_label` declared, no manifest generators)
- Clone: measured in the gate, not wired yet (service rejects `clone-from`)
- Immediate vs. deferred cleanup: this proef uses `IMMEDIATE`, which requires per-resource purge logic in production
- Metrics job to count objects per bucket: doable but adds a periodic task
- Production rollout: needs a cluster-level configuration switch (like `sandboxed-local` has) and per-tenant migration order

## See Also

- `features/seaweedfs-objectopslag.md` – full feature description, gate table (34 rows, all measured), and limitations
- `features/impliciete-dienstselectie.md` – why the service can auto-enroll on deployment
- `docs/KNOWN-ISSUES.md` – why MinIO is an interim measure
