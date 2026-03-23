# System-Wide OOM Watcher

## What it is

A periodic background job that scans all managed namespaces for OOM-killed pods, independent of deploy/refresh events. Currently, OOM detection only happens during deployment (inline detection) or shortly after (fire-and-forget watcher). Pods that start OOM-killing due to runtime conditions (e.g. increased load, data growth) between deployments go undetected until someone manually checks or triggers a tune.

## Why it's needed

The current OOM detection has blind spots:

- **Inline detection**: Only runs during `wait_for_application_synced` — misses OOM that happens hours/days after a successful deploy
- **Fire-and-forget watcher**: Only checks once, shortly after deploy — misses late-onset OOM
- **UI memory check**: Only runs when a user views the project detail page — passive, not proactive

A system-wide watcher would close these gaps by continuously monitoring all deployments.

## Proposed approach

1. **Periodic scan** (e.g. every 5-10 minutes) that iterates all projects/deployments managed by this OPI instance
2. For each deployment, check pods via kubectl for OOM kills (using `lastState.terminated` with reason/exitCode checks, filtered by pod creation timestamp to avoid stale data)
3. When OOM detected, automatically trigger `tune_deployment_resources` with the sliding bump factor
4. Respect the existing `OOM_INLINE_MAX_ATTEMPTS` cap to prevent infinite tuning loops
5. Log and optionally notify (future: webhook/Slack integration) when OOM is detected and tuned

## Considerations

- Must not conflict with inline detection or fire-and-forget watcher — use a shared attempt counter or coordination mechanism
- Should be configurable: enable/disable, scan interval, max auto-tune attempts
- Resource impact: kubectl calls for every deployment every N minutes — may need batching or rate limiting on large clusters
- The Prometheus-based `kube_pod_container_status_last_terminated_reason` metric is unreliable (sometimes reports `Error` instead of `OOMKilled` for exit code 137 kills) — kubectl-based detection is preferred

## Dependencies

- Existing infrastructure: `_check_oom_kills_via_kubectl`, `tune_deployment_resources`, sliding bump factor, OOM floor logic
- Project service for iterating all managed projects/deployments
