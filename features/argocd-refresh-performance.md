# ArgoCD Refresh Performance Issue

## Problem

After the operations manager creates a new project, it triggers a synchronous refresh of the `user-applications` ArgoCD application. This refresh can take **2+ minutes** due to ArgoCD's cluster cache invalidation, blocking the entire project creation flow at ~85% progress.

## Root Cause Analysis (2026-02-27)

### What happens during project creation

1. Operations manager commits ArgoCD resources (Application, AppProject, Repository secret) to git
2. Calls `GET /api/v1/applications/user-applications?refresh=normal` - **this is synchronous and blocks until done**
3. Tries to refresh the individual project application

### Why the refresh is slow

The ArgoCD application controller periodically invalidates its cluster cache (~every 1-1.5 hours). When a refresh request arrives during or just after a cache invalidation, the controller must first complete a full cluster resync before processing the refresh.

**Observed timeline (testw-03o creation):**

| Time | Event |
|------|-------|
| 12:40:11 | Cluster cache invalidated ("Notifying settings subscribers") |
| 12:40:11 | Full cluster resync starts - lists every resource type in every namespace |
| 12:40:25 | Operations manager sends refresh request - **blocked waiting for resync** |
| 12:40:33 | Kubernetes API client-side throttling kicks in (~1s delays per request) |
| 12:42:11 | Cluster resync completes after ~2 minutes |
| 12:42:12 | Refresh finally processed - git fetch 1s, manifest generation 2.6s |

The actual work (git fetch + CMP manifest generation) takes **~4 seconds**. The **~107 seconds** of delay is entirely the cluster resync + API throttling.

### Why the API gets throttled

The controller lists every resource type (including OpenShift CRDs, Kyverno, Elastic, OCS, external-secrets, etc.) across every managed namespace. The Kubernetes API applies client-side rate limiting, adding ~1 second delays per throttled request. With many CRD types and namespaces, these delays compound.

### Current ArgoCD controller settings

```
--status-processors 20
--operation-processors 10
--kubectl-parallelism-limit 10
--repo-server-timeout-seconds 180
```

These are reasonable values. The parallelism doesn't help because the bottleneck is the cluster cache resync which must complete before any reconciliation can proceed.

### Current resource exclusions (argocd-cm)

Only minimal exclusions are configured:
- Endpoints, EndpointSlice
- APIService
- Lease
- SelfSubjectReview, TokenReview

## Impact

- Project creation shows 85% progress for 2+ minutes with no feedback
- The `user-applications` refresh has no HTTP timeout, so the operations manager blocks indefinitely
- If the refresh coincides with a cache invalidation, the delay is unavoidable

## Potential Improvements to Investigate

### Short term (operations manager)

- **Add HTTP timeout** to `_make_authenticated_request` - don't let the refresh block indefinitely
- **Don't block on `user-applications` refresh** - fire the refresh, then poll for the child application to appear instead of waiting for the synchronous response
- **Poll for child app with `wait_for_application_created`** - the infrastructure flow already does this with a 120s timeout; regular apps should too

### Medium term (ArgoCD configuration)

- **Expand `resource.exclusions`** - exclude OpenShift-specific CRDs that projects don't use (machine.openshift.io, ocs.openshift.io, maps.k8s.elastic.co, etc.) to reduce cluster resync time
- **Investigate cache invalidation trigger** - settings notifications happen frequently but only some trigger full invalidation; understand what triggers the actual invalidation

### Long term (architecture)

- **Consider ApplicationSet** - instead of app-of-apps with CMP, use an ApplicationSet with a Git generator to auto-discover project folders; this avoids the heavy `user-applications` manifest generation entirely
- **Webhook-driven sync** - configure GitHub webhooks to notify ArgoCD directly on push, rather than relying on the operations manager to trigger refresh
- **Separate ArgoCD instances** - if the number of projects keeps growing, consider splitting the workload across multiple ArgoCD instances to reduce cache resync scope
