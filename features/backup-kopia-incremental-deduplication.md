# Backup: Kopia Incremental & Deduplication Investigation

**Status**: Future Investigation

This document outlines the investigation into properly leveraging Kopia's incremental backup and deduplication capabilities in the backup system. The current architecture uses Kopia as a full-snapshot engine without benefiting from its core value proposition.

## Problem Statement

The backup system currently spawns stateless pods that connect to a Kopia repository, create a full snapshot, and disconnect. While Kopia internally uses content-addressable storage with block-level deduplication, the stateless pod pattern limits these benefits:

1. **No local cache persistence** - Each backup pod starts fresh. Kopia cannot efficiently compare against prior snapshots without its local index/cache, resulting in re-reading and re-hashing all data blocks every run.
2. **Full upload overhead** - Without awareness of what blocks already exist in the repository, the backup pod may re-upload data that is already stored, or at minimum must re-hash all source data to determine what changed.
3. **No incremental transfer** - True incremental backup requires knowledge of the previous snapshot's block index. Without a persistent cache, each run behaves like a first-time backup in terms of I/O and CPU cost.
4. **Retention is the only feature leveraged** - The system effectively uses Kopia as a retention-managed file uploader with encryption, rather than as a deduplication engine.

### Impact

- **Backup duration**: Each backup reads and hashes the entire source volume, regardless of how little has changed since the last backup.
- **Network bandwidth**: Unchanged data blocks may be re-transferred to S3.
- **S3 storage**: Kopia's repository-level deduplication still provides *some* storage savings (identical blocks within the same repository are stored once), but this is incidental rather than actively optimized.

## Current Architecture

```
┌───────────────────────────────┐
│  Operations Manager           │
│  (orchestrator)               │
│                               │
│  1. Create VolumeSnapshot     │
│  2. Create clone PVC          │
│  3. Spawn backup pod          │
│  4. Wait for completion       │
│  5. Cleanup                   │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Backup Pod (stateless)       │
│                               │
│  kopia repository connect s3  │  ← Connects fresh every time
│  kopia snapshot create /data  │  ← Full read + hash of all data
│  kopia policy set ...         │
│  kopia snapshot expire        │
│  exit                         │  ← Cache/index discarded
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  S3 Repository                │
│  (per namespace prefix)       │
│                               │
│  Repository-level dedup       │
│  exists but is under-utilized │
└───────────────────────────────┘
```

## Investigation Areas

### Option A: Persistent Kopia Cache PVC

Attach a small persistent volume to each backup pod that stores the Kopia local cache and index between runs.

**How it works:**
- A per-namespace (or per-repository) PVC stores Kopia's cache directory (`~/.cache/kopia/`)
- The backup pod mounts this PVC alongside the data PVC
- On subsequent runs, Kopia reads the cached index to determine which blocks already exist in the repository
- Only new/changed blocks are hashed and uploaded

**Considerations:**
- Requires managing an additional PVC per namespace (lifecycle, cleanup, sizing)
- Cache PVC must be writable and available in the same namespace
- If the cache is lost, the next backup gracefully degrades to a full scan (no data loss)
- Cache size is typically small relative to data (index metadata only)

### Option B: Kopia Repository Server

Run a persistent Kopia server that backup pods connect to as clients, instead of connecting directly to S3.

**How it works:**
- A long-running `kopia server start` deployment maintains the repository connection and cache
- Backup pods use `kopia repository connect server` instead of `kopia repository connect s3`
- The server maintains indexes in memory/disk, enabling efficient incremental operations

**Considerations:**
- Adds a new long-running component to manage (deployment, service, health checks)
- Single point of failure unless HA is configured
- More complex security model (server needs access to all namespace repositories, or one server per namespace)
- Kopia server supports multi-user with ACLs, but adds operational complexity
- Better suited for environments with many frequent backups

### Option C: Simplify Away from Kopia

If incremental backup and deduplication are not needed, replace Kopia with direct S3 uploads and custom retention logic.

**How it works:**
- Replace `kopia snapshot create /data` with `mc mirror /data s3://bucket/prefix/` or `tar | aws s3 cp`
- Replace `pg_dump | kopia snapshot --stdin` with `pg_dump | aws s3 cp - s3://bucket/prefix/db.dump`
- Implement retention as a Python cleanup job (delete S3 objects older than N days/keeping N latest)
- Handle encryption with age/gpg wrapper or S3 server-side encryption

**Considerations:**
- Eliminates Kopia dependency entirely
- Simpler to understand and debug
- Loses Kopia's tagging system (would need S3 object tags or naming conventions)
- Loses block-level deduplication permanently (each backup is a full copy)
- Must implement retention logic ourselves
- Encryption at rest needs explicit handling (age pipe, or S3-SSE)
- Acceptable if backups are small or storage cost is not a concern

## Evaluation Criteria

| Criterion | Option A (Cache PVC) | Option B (Server) | Option C (No Kopia) |
|---|---|---|---|
| Implementation complexity | Low-medium | High | Medium |
| Operational overhead | Low (extra PVC) | High (new service) | Low |
| Incremental backup support | Yes | Yes | No |
| Deduplication | Yes (full) | Yes (full) | No |
| Backup speed improvement | Significant for large volumes | Significant | None (already full) |
| Storage savings | Significant | Significant | None |
| Failure mode | Graceful (falls back to full) | Server failure blocks backups | Simple |
| Current architecture impact | Minimal (add PVC mount) | Major restructure | Major restructure |

## Recommended Investigation Steps

1. **Benchmark current behavior** - Measure actual backup duration, data transferred, and S3 storage growth over a week of daily backups for a representative PVC (e.g., 10 GB with ~5% daily change rate).
2. **Test Option A with a single namespace** - Add a cache PVC to one backup pod template and compare duration/transfer for subsequent runs.
3. **Measure the delta** - If Option A shows meaningful improvement (e.g., >50% reduction in duration/transfer for unchanged data), proceed with a broader rollout.
4. **Decide on Option C** - If benchmarks show that backup volumes are small enough that full backups complete quickly and storage cost is negligible, simplifying away from Kopia may be the better path.

## Corrections to Existing Documentation

The current `features/backup-system.md` contains claims that should be revisited after this investigation concludes:

- **"Incremental backups using Kopia's deduplication"** (line 9) - Currently not delivered due to stateless pods.
- **Storage Efficiency table** (lines 920-930) - The table showing incremental upload sizes is aspirational, not reflective of current behavior.
- **"Encrypted, deduplicated backups"** (line 663) - Encryption works; deduplication is under-utilized.

These should be updated to reflect actual behavior, or updated once incremental backups are properly implemented.

## Dependencies

- Kopia documentation on [caching](https://kopia.io/docs/advanced/caching/) and [repository server](https://kopia.io/docs/repository-server/)
- Benchmark tooling to measure S3 transfer size and backup duration
- Access to a representative data set for testing

## Related

- [Backup System](backup-system.md) - Current backup system documentation
- [Storage Metrics Monitoring](storage-metrics-monitoring.md) - Monitoring that can help establish baseline data sizes
