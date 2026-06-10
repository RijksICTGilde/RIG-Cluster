# VPA as Tuning Recommender

**Status**: Idea
**Priority**: Low
**Created**: 2026-06-05

## What it is

Use the cluster's Vertical Pod Autoscaler (now available on ODCN) in `updateMode: "Off"` as the *recommendation engine* for resource tuning, while keeping the existing ZAD apply-flow (project file + Git + ArgoCD) as the only way recommendations are applied.

## Why

We are effectively rebuilding VPA in-house. The auto-tune + oom-watcher combination works well, but it duplicates what VPA's recommender already does — and does one thing worse: it never tunes *down* after an oom-watcher bump.

Real-world case (June 2026): the regel-k4c enrichworker was sized by the oom-watcher in April at 1Gi request / 4Gi limit, based on genuine heavy usage at the time (avg 1142Mi, OOM kills). The workload then went idle (~2Mi, occasional bursts to ~2Gi), but the sizing stuck. Multiplied across 8 PR environments this reserved ~8Gi of paid requests for idle binaries (~EUR 215/month at EUR 27/GiB), and contributed to the tenant-quota exhaustion that blocked deployments cluster-wide on June 4.

VPA's recommender handles exactly this: histogram-based percentile targets over a sliding window, with built-in OOM handling, that decay back down when usage drops.

## Why NOT auto-apply (updateMode Recreate/Auto)

1. **GitOps mismatch**: VPA mutates pods via admission webhook, outside Git. The project file and its tuning history (`source: auto-tune` / `oom-watcher` with `reason:` lines) would no longer reflect reality. That history proved its value during the enrichworker analysis — it explained *why* the sizing existed.
2. **Eviction under a full tenant quota is dangerous**: VPA applies changes by evicting pods. If the Capsule quota is near-full (as on June 4), the replacement pod is denied (`FailedCreate: exceeded quota`) and the workload is down. VPA is not quota-aware.
3. **Burst workloads and short-lived PR environments fit poorly**: recommendations mature over days; PR envs often live shorter. A mid-run eviction of an enrichment worker kills the run.

## Proposed approach

1. ZAD generates a `VerticalPodAutoscaler` manifest per deployment component with `updateMode: "Off"` (recommendation only, nothing applied by VPA itself).
2. The existing auto-tune flow reads `status.recommendation` from the VPA objects instead of (or alongside) its own avg/max calculation, and applies via the project file + Git as today.
3. Keep the oom-watcher as-is for fast reaction; VPA recommendations provide the slow path, including downward correction.
4. Phase 0 (cheap validation): only *log* VPA recommendations next to auto-tune decisions for a few weeks and compare, before changing the apply path.

## What this replaces / keeps

| Piece | Now | After |
|---|---|---|
| Measurement + recommendation | own avg/max + 25% logic | VPA recommender (incl. downscaling) |
| OOM fast-path | oom-watcher | oom-watcher (unchanged) |
| Apply path | project file + Git + ArgoCD | unchanged |
| Audit trail | resources.history in project file | unchanged |

## Dependencies

- VPA installed on the cluster (available on ODCN production as of June 2026).
- Related: `system-wide-oom-watcher.md` (periodic detection), `sidecar-resource-tuning.md`, `configurable-deployment-resources.md`.

## Open questions

- One VPA object per Deployment means N objects per project (PR envs included) — lifecycle must follow the environment lifecycle (create/delete with the env).
- Does VPA's recommender get enough history for short-lived PR envs to beat the current heuristic there, or should PR envs inherit recommendations from the main deployment's VPA?
- minAllowed/maxAllowed policy: derive from the component's current limits, or platform-wide floors/ceilings?
