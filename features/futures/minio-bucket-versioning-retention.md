# MinIO Bucket Versioning Retention

## Status

Open — not yet implemented. Surfaced after the production MinIO PVC filled up and was resized from 2Gi to 10Gi (2026-05-19). Ownership of the offending bucket lies with a user application team, not the platform team.

## Scope

This concerns **user application buckets** in MinIO whose owning team has enabled object versioning without configuring a lifecycle policy. The concrete trigger was the `mb-docs-helmfile-production` bucket, which is provisioned and managed by team `mb`'s helmfile deployment of their Docs (Outline) application — **not by ZAD**.

So this is *not* a ZAD-platform feature to be built. It is platform guidance / a known-issue note for user-app teams who run their own versioned buckets on the shared MinIO instance.

## Background: what happened

- Production MinIO instance ran out of space (2Gi PVC at 100%).
- Drill-down per bucket showed ~95% of usage in `mb-docs-helmfile-production` (~1.84 GiB raw / ~920 MiB unique object data, spread across the 4 EC sub-disks on a single PVC).
- That bucket has 272 distinct object paths, with individual objects holding 30+ versions each.
- Other buckets were effectively empty.

## How MinIO versioning behaves by default

When a bucket has versioning enabled:

1. Every PUT on an existing key creates a new "current" version; the previous version becomes a "noncurrent version" and is **kept indefinitely**.
2. Every DELETE places a "delete marker" as the new current version. The previous content versions are **not removed**.
3. Failed/aborted multipart uploads leave partial data behind.
4. There is **no built-in retention** — MinIO will keep all versions forever until something tells it not to.

In other words: versioning on a long-lived, write-heavy bucket is an unbounded growth pattern by design.

## Where retention is configured

Retention is **not** an application-level concern (the Docs app only decides *when to create a new version* — typically on every save). It is configured on the **MinIO bucket** via standard S3 lifecycle rules.

Relevant lifecycle actions for a versioned bucket:

| Action | Effect |
|---|---|
| `NoncurrentVersionExpiration` (`NoncurrentDays: N`) | Delete noncurrent versions older than N days |
| `NewerNoncurrentVersions: M` | Keep at most M noncurrent versions per object |
| `Expiration.ExpiredObjectDeleteMarker: true` | Clean up dangling delete markers once the last underlying version is gone |
| `AbortIncompleteMultipartUpload.DaysAfterInitiation: N` | Drop failed multipart uploads after N days |

Set via `mc ilm rule add` against the bucket, or the MinIO console under *Bucket → Lifecycle*.

Example baseline for a docs-style application:

```bash
mc ilm rule add minio/mb-docs-helmfile-production \
  --noncurrent-expire-days 30 \
  --newer-noncurrent-versions 10 \
  --expired-object-delete-marker \
  --abort-incomplete-multipart-days 7
```

For most editor/wiki workloads "keep 30 days OR max 10 noncurrent versions per object" is a sensible starting point and would shrink this bucket from ~1.84 GiB to a few hundred MiB.

## Ownership and next steps

- **Bucket owner (team `mb`)**: should decide a retention policy that matches the recovery requirements of Docs (how far back do they want to be able to restore a page?) and apply it via lifecycle rules. They own this.
- **Platform team**: optional follow-ups, ordered by usefulness:
  1. Notify team `mb` of the situation, recommend a policy.
  2. Consider whether *all* MinIO buckets on the shared instance should have a default lifecycle policy applied at provisioning time (only relevant if/when the platform takes over bucket creation for user apps).
  3. Add a Prometheus alert on MinIO bucket size growth or PVC fill-rate, so this doesn't surface as "disk full" again.

## Why this is not a ZAD feature

ZAD currently provisions storage services for projects defined in `projects/*.yaml`. The `mb-docs-helmfile-production` bucket was created outside that flow — directly by team `mb`'s helmfile deployment using its own MinIO admin credentials. ZAD has no knowledge of, or control over, lifecycle rules on user-managed buckets. Building "default lifecycle rules" into ZAD would only apply to buckets ZAD itself provisions, and team `mb`'s bucket would not benefit.

If at some point user apps stop creating their own buckets and start requesting them through ZAD, *then* adding a default `NoncurrentVersionExpiration` policy at provisioning would be a reasonable platform-side improvement — but that is a different feature from this note.
