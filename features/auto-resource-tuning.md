# Auto Resource Tuning

**Status**: Implemented (on-demand + nightly, VPA-driven memory + CPU)
**Created**: 2026-02-10
**Updated**: 2026-08-20

## Overview

The auto resource tuning system computes recommended Kubernetes resource requests and limits and writes them into project YAML files. Changes flow through git, so ArgoCD deploys them like any other configuration change.

Recommendations come from one of two sources:

- **VPA recommender** (on clusters where `supports_vpa` is true, e.g. `odcn-production`): an Off-mode `VerticalPodAutoscaler` is generated per component. The platform's recommender publishes CPU **and** memory recommendations to its `.status`, which the tuner reads. It is the only source that tunes CPU. For memory it is used **only when its target exceeds the recommender's built-in floor** (`VPA_MEMORY_FLOOR_MI`, 250Mi); the upstream recommender never advises below that floor, so a target sitting at it means "real usage is below the floor", not a genuine need (see Recommender Floor below).
- **Prometheus** (fallback on non-VPA clusters, for components whose VPA has no recommendation yet, **and whenever the VPA memory target is at the floor**): the historical memory-usage window described below. Memory only.

Because the VPA target already carries its own percentile + safety margin, the tuner adds **no buffer of its own on top of a VPA memory target**. The percentage buffer and the flat 25Mi headroom apply **only to raw Prometheus measurements**.

Available both as an on-demand API endpoint (`POST /api/resources/{project_name}/tune`) and as a nightly background sweep that tunes the whole estate (see Nightly Auto-Tuning).

## How It Works

```
  VPA recommender .status              Prometheus (fallback + OOM detection)
  (memory + CPU: target/bounds)        (max_over_time memory, OOMKilled reason)
              \                          /
               v                        v
          Resource Analyzer  (memory + CPU recommendation)
                         |
          request+buffer -> deadband gate -> OOM floor -> min/max clamps
                         |
                         v
   Project YAML  --(git commit)-->  reprocess  -->  ArgoCD  -->  pod rollout
   (deployment override only; root component untouched)
```

### Tuning Flow

For each component in the target deployment(s):

1. Skip if opted out (`auto-tune-resources: false`).
2. **Repair first**: if the current override already sits below the declared root, restore it to the root and stop there (see Root Component below). This happens before anything is measured and before the guard below, because a component starved by such an override is exactly the one that neither of them can reach.
3. Skip if the deployment is not Available — **except on the OOM path**, where the not-Available guard is deliberately skipped (a component that just OOM'd is Available=False by definition, and that is exactly when it must be raised).
4. Determine the recommendation source:
   - **CPU**: if the cluster has VPA and the component's `VerticalPodAutoscaler` has a populated `.status` → use its CPU `target`. (No Prometheus CPU path exists.)
   - **Memory**: use the VPA memory `target` **only if it exceeds `VPA_MEMORY_FLOOR_MI`** (the recommender's floor). Otherwise (no VPA, empty `.status`, or target at the floor) fall back to Prometheus `max_over_time(container_memory_working_set_bytes{...})` over `window_hours`.
   - OOM kills are always read from Prometheus (`kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`); when OOM kills are present the VPA target is not used (the OOM path drives the limit instead).
5. Skip if the observed max is below `min_observed_mi` (see Plausibility Floor below), unless OOM kills were detected.
6. Compute the recommendation (analyzer), apply the deadband gate, OOM floor, and clamps.
7. **Leave the fields the user set by hand alone** for as long as that intent lives (see
   `features/handmatig-gezette-resources.md`). Per field, not per component: a pinned CPU
   does not stop memory from being tuned. The one exception is an active OOM kill, which
   may still raise the memory *limit*.
8. Write changed values to the **deployment-level override only**. The base (root) component is left exactly as the user declared it — it is not ratcheted by the tuner (see Root Component below).
9. Commit once per project, then reprocess so ArgoCD redeploys.

### Recommendation Algorithm (Memory)

Tuning drives the **request** (the reserved memory that counts against cluster
capacity); the limit is treated as a ceiling:

- **Request**:
  - **Prometheus path**: `peak * (1 + buffer%)`, where `peak` is the Prometheus
    `max_over_time`. Apps using >= 100Mi get an extra flat 25Mi headroom on top of
    the percentage buffer.
  - **VPA path**: the VPA `target` is taken **as-is, with no buffer and no 25Mi
    headroom**: it already includes the recommender's own margin. (The reason
    string reads `Request: VPA target <N>Mi = <N>Mi` rather than `max ... + 25%`.)
- **Limit** = `observed peak * RESOURCE_TUNING_MEMORY_LIMIT_FACTOR` (default 1.5x),
  never below the request. The limit is **not frozen**: it decays as the peak
  decays, instead of being held forever at a value a prior one-time spike (or OOM
  bump) left behind. Two things keep this safe:
  - A **valid OOM floor** is the lower bound (see OOM Kill Handling). A real,
    recent peak is remembered and protects the limit; only when that floor
    expires (stale: old enough and usage since stayed well below it) does the
    limit drop toward `peak * factor`.
  - The **OOM watcher** is the reactive net for boot/startup spikes that fell
    *outside* the observation window. We cannot size for a spike we never saw, so
    if the limit decayed too far, the next restart OOMs once, the watcher bumps it
    back, and the floor remembers it. This is the deliberate trade for not
    permanently over-provisioning every workload.

Why peak-based and not an absolute floor: a flat minimum limit (e.g. 256Mi) would
over-provision genuinely small pods (an nginx sidecar at 25Mi does not need a
256Mi ceiling). Sizing the limit from the observed peak lets it scale with the
workload in both directions.

Subject to the cluster memory minimum (default 25Mi) and maximum; the request is
capped to never exceed the limit; and the **deviation deadband** (below, checked
on request *and* limit independently) decides whether the change is worth
committing.

### Recommender Floor

The upstream VPA recommender never recommends below a built-in minimum
(`podMinMemoryMb`, 250Mi by default; `podMinCPUMillicores`, 25m). When a workload
genuinely uses less than that, the recommender clamps its `target` **up** to the
floor. So a memory target of exactly 250Mi does not mean "this pod needs 250Mi":
it means "this pod uses less than 250Mi and the recommender won't say how much
less". Taken at face value this over-provisions every small workload, which is the
common case here.

To keep memory requests usage-based, the tuner treats a VPA memory target at/below
`VPA_MEMORY_FLOOR_MI` as **no usable signal** and falls back to the Prometheus
measurement (the real usage), which has no such floor. `VPA_MEMORY_FLOOR_MI` is a
plain mirror of the recommender's `--pod-recommendation-min-allowed-memory-mb`
flag: if the platform changes that flag, this constant must change with it.

This refinement is **memory-only** by design (see the note under the CPU
algorithm). Lowering the recommender's floor platform-wide, the alternative that
would also fix CPU, lives outside this repo (it is a recommender deployment flag).

### Recommendation Algorithm (CPU)

CPU is tuned only on the VPA path (the Prometheus fallback leaves CPU untouched).
CPU is compressible - it throttles rather than OOM-kills - so there is no floor
or emergency path:

- **Request** = VPA cpu `target * (1 + buffer%)`, clamped to `[min_cpu_m, max_cpu_request_m]` (25m..250m on odcn).
- **Limit**: mirrors the request when the two were equal (the untouched default);
  a limit already set to differ from the request is left **frozen**. Clamped to
  `max_cpu_limit_m` (4000m on odcn).

That freeze is a **guess**, and it guesses wrong in both directions: a tuner-set pair that
happens to differ looks frozen too, and a user who deliberately sets limit equal to request
loses the protection. Since RC-141 a value the user actually set carries a `manual` history
entry and is skipped before this function ever sees it, so the guess only applies to
components with no recorded intent. It stays because removing it would let the tuner pull
every limit down to its request in one sweep (in production nearly every component has
limit != request), and it can go once intent is recorded broadly. See
`features/handmatig-gezette-resources.md`.

Note: the memory-only refinements do **not** apply to CPU. CPU keeps the
frozen-limit rule (memory dropped it in favour of a decaying peak-based limit),
still adds the buffer on top of the VPA target, has no Prometheus fallback, and
has no `VPA_MEMORY_FLOOR_MI` equivalent. So a component whose CPU target sits at
the recommender floor stays at `floor + buffer` (e.g. `25m + 25% ≈ 31m`). CPU is
compressible and cheap, so this asymmetry is accepted.

### No manual UI tuning

There is **no per-deployment memory-check fragment or manual "Tune resources"
button** in the project UI. It was removed once tuning became fully automatic:
reductions are applied by the nightly sweep, increases by the reactive OOM
watcher, so a read-only advice card plus a manual trigger only duplicated what the
system already does (and the card surfaced sub-deadband savings the tuner ignores,
which read as inconsistent). Removing it also dropped an expensive per-card
Prometheus + kubectl call on every project-page load.

What still surfaces a problem: the deployment card's **ArgoCD health badge**
(a crashing or OOM pod shows `Degraded`). The one case this no longer spells out
explicitly is "OOM while already at the cluster max" (needs manual intervention);
the proper home for that is service-level monitoring/alerting, not a page-load
call. Programmatic tuning remains available via `POST /api/resources/{project}/tune`.

### OOM Kill Handling

OOM-killed containers produce misleading usage data (the pod was killed before reaching its true peak). When OOM kills are detected:

- The **not-Available guard is skipped** (`oom_triggered`), so the OOM path is not blocked precisely when it needs to raise the limit.
- Only the component(s) that actually OOM'd are analysed (a targeted tune, no wasted Prometheus queries on healthy components).
- The limit is set using a sliding factor: at least **3x** the current limit below 64Mi, 2x below 256Mi, else 1.5x — regardless of observed usage.
- If the pod was OOM-killed on startup with zero Prometheus metrics, the current YAML values are used as a baseline.
- OOM kills bypass the change threshold - any OOM kill triggers an update.

### Root Component

Resource tuning writes to **deployment-level overrides only**. The base (root) component is the value the user declared, not shared state the tuner ratchets. Writing tuned values back to the root used to be a last-writer-wins race: on `asses-k2n/api` the nightly sweep pulled the shared limit from 75Mi (measured on production) to 45Mi (measured on a much lighter PR) within six seconds, and every new PR then started at 45Mi and OOM'd. So the tuner no longer touches the root at all.

A new deployment therefore inherits exactly the declared root. If that is too tight it OOMs once and the OOM watcher raises **that deployment's own override** — the root stays put.

The root is also the **floor**: an override the tuner writes is never tuned below the memory the user declared on the component (a lower bound only — a deployment may always raise itself above it).

An override that already sits below the root is **repaired** at the top of the tuning flow, before any measurement and ahead of the not-Available guard. Such an override starves the component, and that starvation blocks both routes that would otherwise correct it: there is no running pod to measure, and the deployment reports Available=False. The repair restores the declared value (only the deficient side of it), which needs neither. Deployment-level overrides are written by the tuner alone — there is no editable or API for them — so a repair can never overwrite a value someone set on purpose.

If the component was auto-disabled for an OOM kill, the repair also **clears that disable**. Leaving it would make the repair invisible and permanent: a component scaled to zero has no pods, so no OOM metric, so nothing that would ever switch it back on. Same shape as the image-pull disable, which clears once the image changes. Disables for any other reason are left alone: memory says nothing about a missing image.

**Veldgeval mpfpsm-lcl pr-200** (14 August 2026): a WireMock stub measured as 0Mi during the sweep, was sized to the cluster minimum of 25Mi, and was then OOM-killed before its first log line. Every route back was closed: the pod could not run, so the next sweep measured 0Mi again; `:refresh` and a new image tag did not help because the value sat in the project spec, not in the manifest. Repairing to the declared root is the way out that needs no measurement.

### Plausibility Floor

An observed max below `min_observed_mi` (5Mi) counts as **no data**, not as a real measurement, and the component is skipped (unless OOM kills were detected, which takes the OOM path with the current YAML values as baseline).

This replaces an exact-zero test that was meant to catch the same thing and did not. A pod that existed only briefly inside the window reports a fraction of a Mi: that is not zero, so it passed, and it prints as `0Mi` in the reason line, which is what `mpfpsm-lcl/pr-200` recorded (`Request: max 0Mi + 25% = 25Mi`). Any container that really ran passes 5Mi within seconds, so nothing legitimate is lost.

### Limit/Request Margin

A written memory limit always stays **at least `RESOURCE_TUNING_MIN_LIMIT_HEADROOM_MI` (64Mi) above the request**, capped by the cluster limit. A container with `limit == request` has no burst headroom and dies on the first spike — the failure mode behind the headscale OOM cascade (a 25Mi==25Mi component killed four times in two minutes). An absolute margin is used rather than a factor, because a factor on a small measurement rounds request and limit to the same value, exactly where headroom is needed most.

## API

### Tune Resources

```
POST /api/resources/{project_name}/tune?deployment={deployment_name}
```

- `deployment` is optional - omit to tune all deployments in the project
- Requires API token authentication

Response:
```json
{
  "project": "my-project",
  "changes": [
    {
      "component": "api",
      "deployment": "production",
      "previous_limits_memory": "512Mi",
      "new_limits_memory": "150Mi",
      "previous_requests_memory": "512Mi",
      "new_requests_memory": "150Mi",
      "max_observed_memory_mb": "100",
      "avg_observed_memory_mb": "80",
      "has_oom_kills": "False",
      "reason": "Request: max 100Mi + 25% + 25Mi headroom = 150Mi. Limit: max 100Mi x 1.5 = 150Mi"
    }
  ],
  "unchanged": [],
  "deployment_refresh_triggered": true
}
```

### Sanitize Deployment

```
POST /api/resources/{project_name}/sanitize?deployment={deployment_name}
```

Detects broken deployments (crash loops, missing images) and disables them by setting `disabled: true` in the project YAML.

An OOM kill on its own is **not** a reason to disable. OOM is what this tuner repairs, and disabling takes away the pods whose OOM metric is the only signal it reads, which turns a memory set too low into a permanent outage (`mpfpsm-lcl/pr-204`). A component that keeps dying for it still trips the restart threshold.

## Configuration

The tuning parameters are **owned by the resource-tuning system service**, not the
platform `Settings`: they live as a validated config dict in
`opi/services/catalog/resource_tuning/config.py` (values) + `config_model.py` (types).
They are deliberately **not** environment-driven — they have never been set via env
vars in practice, and a system service owns its own config. Change a value in
`config.py`; there is no `RESOURCE_TUNING_*` env var any more.

| Field | Default | Description |
|-------|---------|-------------|
| `window_hours` | `24` | Prometheus lookback window |
| `memory_buffer_percent` | `25` | Headroom above the Prometheus measurement (memory request). Not applied to a VPA memory target; still applied to the VPA CPU target |
| `memory_limit_factor` | `1.5` | Memory limit = observed peak x this factor (burst headroom). The limit decays with the peak; a valid OOM floor is the lower bound |
| `increase_threshold` | `10` | Apply an increase when the request grows by ≥ this % |
| `decrease_threshold` | `30` | Apply a decrease only when the request shrinks by ≥ this % |
| `min_delta_mi` | `16` | Ignore memory changes smaller than this (absolute deadband) |
| `min_delta_m` | `10` | Ignore CPU changes smaller than this in millicores (absolute deadband) |
| `min_limit_headroom_mi` | `64` | Minimum absolute headroom the memory limit keeps above the request (so limit never equals request) |
| `min_observed_mi` | `5.0` | Below this observed max the measurement counts as "no data" instead of as a real value |
| `user_intent_min_age_days` | `10` | A value a user set by hand may expire after this many days... |
| `user_intent_stable_percent` | `50` | ...if the measured usage stays below this percent of what they set |
| `scheduler_enabled` | `true` | Run the nightly fleet-wide tuner |
| `hour` | `1` | Hour (Europe/Amsterdam) of the nightly sweep (off-peak, before backups) |
| `pace_seconds` | `15` | Delay after each changed project, to spread pod rollouts |

`VPA_MEMORY_FLOOR_MI` (`250`, in `Settings`) stays where it is: it mirrors the
recommender's `--pod-recommendation-min-allowed-memory-mb` flag rather than being a
tuning knob. A VPA memory target at/below it is treated as "no signal" and the tuner
falls back to Prometheus.

Cluster-specific bounds live in `cluster_config.py`: memory via `get_min_memory_limit_mi()` / `get_max_memory_limit_mi()` / `get_max_memory_request_mi()`, CPU via `get_min_cpu_m()` (25m) / `get_max_cpu_request_m()` (250m) / `get_max_cpu_limit_m()` (4000m), and the `supports_vpa` capability flag.

### Deviation Deadband

A change must clear **both** an absolute floor and a percentage threshold, so the tuner never churns on insignificant drift:

- **Absolute floor** (`RESOURCE_TUNING_MIN_DELTA_MI` / `_M`): ignore changes of only a few Mi / millicores, so tiny pods near the cluster minimum don't get resized every sweep over a handful of megabytes.
- **Percentage** (asymmetric): an **increase** applies at ≥ `RESOURCE_TUNING_INCREASE_THRESHOLD` (react promptly - reliability); a **decrease** must clear the larger `RESOURCE_TUNING_DECREASE_THRESHOLD` (conservative - a small reclaim isn't worth a pod rollout).

OOM-driven increases bypass the deadband entirely. The deadband is also what makes an explicit cooldown unnecessary: a right-sized project's recommendation stays within the deadband of its current size, so the nightly sweep simply produces no change for it.

### Opt-Out

Auto-tuning is **on by default**. A component opts out with `auto-tune-resources: false`, settable at the component-definition level or overridden per deployment-component (deployment override wins).

## Key Files

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Pure computation: usage/VPA target → memory & CPU recommendation, asymmetric gate |
| `opi/services/resource_tuning_service.py` | `apply_resource_tuning` (mutate project_data in place: analysis, root floor, margin) + `tune_deployment_resources` (git read + single commit) |
| `opi/services/catalog/resource_tuning/` | The resource-tuning **system service**: its owned config (`config.py`/`config_model.py`) and its `@on(ActionEvent.AFTER_SYNC)` handler |
| `opi/services/deployment_observation.py` | Generic after-sync runner: asks `registry.listeners(ActionEvent.AFTER_SYNC)`, lets each service observe, commits once |
| `opi/core/resource_tuning_scheduler.py` | Nightly fleet-wide tuner (off-peak sweep + rollout pacing) |
| `opi/connectors/vpa.py` | Parse VPA `.status.recommendation` (CPU→m, memory→Mi) |
| `opi/api/resource_router.py` | On-demand API endpoint |
| `opi/handlers/project_file_handler.py` | YAML manipulation: read/write resources, opt-out flag |
| `manifests/vpa.yaml.jinja` | Off-mode VPA generated per component on VPA-capable clusters |
| `opi/connectors/prometheus.py` / `grafana_prometheus.py` | Prometheus connectors (fallback source) |

## Constraints

### Tenant Cluster

The production environment (ODCN) is a tenant cluster - no cluster-admin permissions. The platform already runs the OpenShift VPA Operator, so creating namespace-scoped `VerticalPodAutoscaler` objects (in `updateMode: "Off"`) is fully within tenant permissions. Off-mode VPAs are advice-only: they never evict or mutate pods, so they create no ArgoCD drift. (Only VPA *Auto/Recreate* mode - which mutates pod specs - is ruled out, because it would conflict with GitOps.)

### GitOps Compatibility

All changes flow through git commits. The tuner reads recommendations (from the VPA `.status` or Prometheus) and writes the resulting requests/limits into project YAML files, which ArgoCD then deploys. Pod specs are never mutated directly.

## Safety Mechanisms

| Mechanism | Purpose |
|-----------|---------|
| **Cluster minimum** | Never set memory below 25Mi |
| **Deviation deadband** | Only commit when the change clears both a % threshold (10% up / 30% down) and an absolute floor (16Mi / 10m) |
| **OOM kill priority** | Always increase memory when OOM kills are present; the not-Available guard is skipped on the OOM path so it never blocks a needed bump |
| **Root left untouched** | The tuner writes only deployment overrides; the declared root component is never ratcheted (no last-writer-wins race across deployments) |
| **Declared root as floor** | An override is never tuned below the memory the user declared on the component (lower bound only) |
| **Limit/request margin** | A written memory limit stays at least 64Mi above the request, so a container never ends up with limit == request |
| **OOM floor (memory limit)** | A valid (recent, not-yet-stale) OOM floor is the lower bound for the memory limit, so a real past peak is never undercut; the limit only decays once the floor expires |
| **OOM watcher net** | The reactive OOM watcher re-bumps a limit that decayed too far (e.g. an unobserved boot spike), so the peak-based limit can shrink without permanently risking startup |
| **Frozen limit (CPU only)** | A CPU limit already set to differ from the request is left untouched; memory limits are no longer frozen (they track the peak). This is a GUESS and only the fallback for components with no recorded intent |
| **User intent wins** | A field a user set through the portal or API carries a `manual` history entry; the tuner skips exactly those fields until the intent expires. Only an active OOM kill may still raise the memory limit |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Deployment-level scoping** | Tuning writes to deployment overrides, not shared definitions (except request propagation) |
| **Fresh git reads** | Tuning reads the latest YAML from git before modifying, preventing stale cache data from overwriting concurrent changes |
| **Legacy key migration** | Flat `cpu`/`memory` resource keys are migrated into nested `requests`/`limits` before removal, preventing silent data loss |

## Nightly Auto-Tuning

`ResourceTuningScheduler` (`opi/core/resource_tuning_scheduler.py`, started from the server lifespan) runs **once a night** at `RESOURCE_TUNING_HOUR` (Europe/Amsterdam, default 01:00 — off-peak, and an hour before backups (~02:00) so resize-triggered pod rollouts settle before the backup snapshot). Each night it:

1. Enumerates every project with a deployment on this OPI's cluster.
2. Calls `tune_deployment_resources(project)` for each. The deadband decides what actually changes; converged projects produce no change and cost only the (cheap, read-only) analysis.
3. Paces `RESOURCE_TUNING_PACE_SECONDS` after each project that changed, so a convergence night spreads its pod rollouts instead of bouncing the whole fleet at once.

Why a nightly full sweep rather than a paced/capped/cooldown schedule: **checking is cheap** (VPA `.status` reads + a few Prometheus queries, no writes); the real cost — commit, reprocess, ArgoCD sync, rollout — is only paid when a project drifts past the deadband. So cost scales with how many projects *drift*, not how many are checked, and the deadband (not a cooldown) is what prevents night-to-night churn. Urgent under-provisioning between sweeps is handled out-of-band by the reactive OOM watcher.

## Known Limitations

These are inherent to reactive, history-based autoscaling and are worth knowing before trusting the tuner blindly:

- **Idle-then-spike / boot-spike risk.** The recommendation reflects only what was observed in the window. A workload that is idle (or simply not restarted) during the window and then boots or does something new is sized too low, and the first spike can OOM-kill it. The memory limit is now `peak x limit_factor` (default 1.5x) rather than collapsed onto the request, which gives burst headroom *above the observed peak*; but it cannot cover a spike that was never observed at all (a boot that did not happen inside the window). That residual case is handled reactively: the limit OOMs once on the next restart, the OOM watcher bumps it, and the OOM floor remembers it. This is the deliberate trade for not permanently over-provisioning. The boot spike is fundamentally an observation gap, not something a recommender (ours or the VPA) can size for in advance.

- **Window sensitivity.** Memory sized to `max_over_time` over a fixed window depends on what that window captured: one that caught a rare spike sizes generously, one that missed it sizes too lean. The VPA path (a decaying ~8-day histogram) is more robust, but only contributes above the recommender floor (see Recommender Floor); for the many workloads that use less than the floor, Prometheus remains the effective source for memory requests.

- **Recommender floor on CPU.** The `VPA_MEMORY_FLOOR_MI` fallback fixes memory only. CPU has no Prometheus path, so a component using less than `podMinCPUMillicores` (25m) is sized at `floor + buffer` (~31m) regardless of actual usage. Accepted because CPU is compressible and cheap; the platform-level fix is lowering the recommender's CPU floor flag.

- **OOM recovery timing gap.** When a freshly-resized pod OOMs, the reactive recovery re-derives `has_oom_kills` from the Prometheus OOM metric, which lags the kill by a scrape interval. The already-known OOM signal is now passed straight from the sync / OOM-watcher into the tune (`oom_components`, `oom_triggered`), so the not-Available guard and the sliding bump fire regardless of the metric lag — the case that produced "OOM detected but auto-tune could not determine new limits" on `asses-k2n/pr-450`. A residual lag only remains if the Prometheus OOM metric is *also* needed for the sliding-factor sizing, which uses the current limit as the baseline when no metrics exist.

- **Net effect depends on provisioning.** On an *under*-provisioned fleet (many components at low defaults running real workloads), the tuner raises more than it lowers - net reserved memory goes *up*. That is reliability, not savings; the memory-reclaim story only pays out on an *over*-provisioned fleet.

## After-sync hook and the system service

Resource tuning is a **system service** (`ServiceKind.SYSTEM`): it always runs, never
appears in a project's `services` list, and is not shown in the wizard's service
picker. It plugs into a generic **after-sync hook** rather than being hardcoded in the
deploy code:

- `ActionEvent.AFTER_SYNC` (an enum, never a string) fires once per deployment after the
  sync. `registry.listeners(ActionEvent.AFTER_SYNC)` returns the services that declared a
  handler for it with `@on(...)`, filtered by `Service.applies_to()` (a system service
  applies to every project).
- `deployment_observation.run_after_sync_observation()` reads the project fresh from
  git, builds a `DeploymentObservationContext` (per-component `ComponentHealth`), lets
  each applicable service observe and mutate `project_data`, and **commits once** for
  all outcomes together — so two services on the hook cannot race to two commits.
- Both callers go through this one scan: the inline deploy path
  (`project_manager`, on `DeploymentHealthError`) and the fire-and-forget watcher
  (`oom_watcher`). Neither names the resource-tuning service.

The resource-tuning service's after-sync handler (`tune_after_oom`) tunes only the
components that OOM'd (via `apply_resource_tuning`), compacts the resource history, and
reports whether a refresh is needed. Image-pull and crash-loop handling remain inline for
now.

See `instructions/services.md` for the service system and `features/oom-kill-watcher.md`
for the health watcher.

## Related

- `features/handmatig-gezette-resources.md` - how a hand-set value beats the tuner, and when it expires
- `features/oom-kill-watcher.md` - the health watcher that detects OOM/image-pull/crash-loop
- `features/futures/sidecar-resource-tuning.md` - extends tuning to sidecar containers
- `features/futures/configurable-deployment-resources.md` - prerequisite for resource values in YAML
- `features/futures/system-wide-oom-watcher.md` - OOMs that arise after a successful deploy (different scope)
