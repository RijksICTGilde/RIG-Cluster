# 0002 - Wait for ArgoCD sync completion after project refresh

**Status**: Accepted
**Date**: 2026-03-11

## Context

When OPI processes a project refresh, it generates Kubernetes manifests, pushes them to Git, and then triggers ArgoCD to sync the applications. Previously, the ArgoCD sync was fire-and-forget: OPI would call `refresh_application()` for each deployment and immediately return success, even if the applications hadn't synced yet (or failed to sync).

This caused two problems:

1. **Race condition on first deployment**: ArgoCD Application resources are managed via a parent `user-applications` app. After pushing new Application manifests, OPI tried to refresh the child applications before ArgoCD had created them from the parent, resulting in "Permission denied" errors that were silently swallowed.

2. **False success reporting**: The refresh task reported completion to the user while pods were still being created (or failing). The user had no visibility into whether their deployment actually succeeded.

## Decision

OPI now waits for ArgoCD sync completion as the final step of project processing:

1. **Refresh `user-applications`** - triggers ArgoCD to detect new Application manifests
2. **Wait for each application to be created** - polls until ArgoCD has created the Application resources (120s timeout)
3. **Refresh each application** - triggers ArgoCD to sync manifests from Git
4. **Wait for each application to be synced and healthy** - polls until `sync=Synced` AND `health=Healthy` (300s timeout)
5. **Report per-application failures** - if one app fails, the others still proceed; all failures are collected and reported

Progress is tracked via the task progress system, so the user sees which application is being synced.

### Why the refresh API is sufficient (no revision pinning needed)

We considered comparing the Git commit SHA against `status.sync.revision` to verify ArgoCD synced the exact revision we pushed. This was deemed unnecessary because:

- The ArgoCD refresh API (`GET /applications/{name}?refresh=normal`) is **synchronous**: it returns the application state *after* ArgoCD has re-read the Git source. By the time the response comes back, ArgoCD knows about the new manifests.
- If manifests changed, ArgoCD transitions to `OutOfSync`, and auto-sync picks it up. We then wait for `Synced + Healthy`.
- If manifests didn't change, the app is already on the correct revision, and returning `Synced + Healthy` immediately is correct.
- The only theoretical risk is ArgoCD's internal refresh cache, but the API contract guarantees a fresh read.

If we encounter edge cases where this assumption breaks (e.g., ArgoCD caching issues or eventual consistency under load), we should revisit and add revision pinning.

### Terminal failure detection

The wait logic detects terminal failures to avoid waiting the full timeout:

- **Sync failures**: `operationState.phase` in `Failed` or `Error` - raises immediately
- **Health degraded**: `health.status == Degraded` - raises immediately
- **Permission denied**: treated as transient (AppProject may not be synced yet) - retries until timeout

## Consequences

**Easier:**
- Users get accurate completion status - when the task says "done", pods are running
- Sync failures are surfaced immediately instead of being discovered later
- The task progress UI shows which application is being waited on

**Harder:**
- Project refresh takes longer (adds sync wait time, typically 5-30s per application)
- If ArgoCD is slow or unhealthy, refreshes may time out (300s per app, 120s for creation)
- Multiple applications are waited on sequentially, not in parallel (keeps the logic simple and avoids overwhelming ArgoCD)

**Future considerations:**
- Could parallelize the wait for multiple applications if sequential waiting becomes a bottleneck
- Could add revision pinning if the synchronous refresh assumption proves unreliable
- The 300s timeout may need tuning based on production experience with large deployments
