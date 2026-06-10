# Deployment TTL Auto-Suspend

**Status**: Idea
**Priority**: Medium
**Created**: 2026-06-09

## What it is

A configurable **time-to-live (TTL)** per deployment. After a deployment has run untouched
for its TTL window, the platform **scales it to zero** (suspend) to free compute — it is
**not deleted**. Databases, buckets and PVCs stay intact. Any explicit re-enable, or any
change to the deployment (new image / config / `:upsert-deployment` call), **resets the TTL
timer** and brings the workload back up.

The primary use case is PR-preview / review environments that stay open for weeks but only
get looked at occasionally. They should run for a while, then idle down automatically, and
come back the moment someone pushes a new image or re-enables them.

## Why

We already have evidence that idle PR environments waste paid quota. The VPA case
(`vpa-as-tuning-recommender.md`, June 2026) found ~8Gi of requests reserved across 8 idle PR
environments (~EUR 215/month), contributing to the tenant-quota exhaustion that blocked
deployments cluster-wide on June 4.

That feature attacks *sizing* (make idle pods smaller). This feature attacks *lifetime*
(turn idle environments off entirely). They are complementary: VPA/auto-tune right-sizes a
running workload; TTL suspends a workload nobody is using. Suspend recovers far more — 100%
of the request, not a fraction — for environments that are genuinely dormant.

Important: this is **suspend, not delete**. It is deliberately distinct from
`yaml-diff-driven-deletion.md` (which marks-and-purges removed resources). Deletion is
destructive and irreversible within the grace window; TTL suspend is cheap to reverse and
keeps all state.

## How it works

```
project file: deployment.type: ephemeral + ttl: "7d"
        |
        v
TTL reconcile (admin-triggered, external cron)   <-- periodic
        |
   for each deployment that is type==ephemeral AND has a ttl
   AND is not the canonical/domain-owning deployment:   (guards, see §2)
     expires_at = last_deployed_at + ttl
     if now > expires_at and not already suspended:
        set replicas: 0 in generated manifests --> commit --> ArgoCD sync
        record suspended state
        |
        v
   deployment scaled to zero (pods gone, data kept)

  any deployment change (upsert-deployment / project sync):
     update last_deployed_at = now     <-- resets the timer
     restore replicas if currently suspended
```

### 1. Configuration (project file + API)

Two new optional fields on the deployment (`DeploymentModel`), both following the existing
`data-retention-period` pattern (string, kebab-case alias):

```yaml
deployments:
  - name: pr-123
    cluster: odcn-production
    type: ephemeral    # persistent (default) | ephemeral
    ttl: 7d            # 7 days of no changes -> scale to zero
    components: [...]
```

- **`type`**: `persistent` | `ephemeral`. **Default `persistent`.** Only `ephemeral`
  deployments can be auto-suspended (see §2). A production deployment is `persistent` (the
  default), so it is structurally exempt — you cannot even attach a `ttl` to it.
- **`ttl`**: format `^[0-9]+(h|d|w)$` (e.g. `24h`, `7d`, `2w`). Empty / omitted = no TTL =
  never auto-suspended. Editable via the project file (Git) **and** the deployment API, so an
  open PR can be given/extended a TTL without a file edit.

Files to touch:
- `opi/forms/models/project_file.py` — add `type` (`Literal["persistent", "ephemeral"]`,
  default `"persistent"`) and `ttl` (`str | None`, default `None`) to `DeploymentModel`,
  next to `data_retention_period`. Add a model validator: `ttl` is only allowed when
  `type == "ephemeral"` (reject otherwise — see §2, guard 1).
- `opi/schemas/project_v2.json` — add `type` (enum) and `ttl` (regex pattern) to the
  deployment object.

### 2. Scope and safety — never suspend production

The whole feature hinges on one guarantee: **a production deployment is never scaled.** That
guarantee is structural, not conventional, via three independent guards (defense in depth):

1. **Schema validation** — `ttl` is only valid on a `type: ephemeral` deployment. A `ttl` on
   a `persistent` (default) deployment is a validation error, so production can't even carry
   the config. Markers default to the safe value: a deployment with no `type` is `persistent`.

2. **Reconciler guard** — the TTL reconcile job suspends a deployment only if it is
   *explicitly* `type: ephemeral` **and** has a `ttl`. Anything not explicitly ephemeral is
   skipped, even if a `ttl` somehow leaked onto it. Belt and suspenders with guard 1.

3. **Canonical guard** — the reconciler additionally refuses to suspend the deployment that
   owns the project's primary/custom domain (`subdomain` / base-domain) or that has no
   `clone-from` (i.e. the canonical root deployment), regardless of `type`. This catches a
   mis-marked root deployment before it ever scales.

PR-preview deployments created via `POST /api/projects/{name}/:upsert-deployment` are set to
`type: ephemeral` **by OPI itself** in that flow (OPI knows it is creating a preview) and get
a **platform default TTL** (proposed: `7d`) when none is supplied. The default is:
- overridable per call (API parameter), and
- overridable per project (a project-level `pr-preview-ttl` setting), and
- disableable by passing an empty/`none` value.

Note: marking is **explicit, never auto-detected**. We do not infer "this looks like a PR
env" from the name (`pr-*`) at suspend time — a misdetection there would scale production. The
only place OPI sets the marker automatically is the upsert flow, where the intent is known.

This keeps the safe default ("review envs idle down after a week") while making it impossible
to auto-suspend a production deployment by accident.

### 3. Suspend mechanism — `replicas: 0` via GitOps

Suspension is expressed **in the generated manifests**, not via a live `kubectl scale`. The
reconcile job sets `replicas: 0` in the deployment's generated manifest in the
`zad-deployments` repo and commits; ArgoCD syncs it. This keeps Git as the single source of
truth and survives ArgoCD self-heal (a raw `kubectl scale` would be reverted on next sync).

This reuses the existing **update project/manifests → Git commit → ArgoCD sync** path that
auto-resource-tuning already uses, so no new apply mechanism is introduced.

Mechanics:
- The deployment template (`manifests/deployment.yaml.jinja`, `replicas: {{ replicas }}`)
  already takes replicas as a variable — suspend just renders `0`.
- The pre-suspend replica count is stored in the TTL state (see §5) so resume restores the
  exact value.
- Suspend touches **only the workload's compute** (the Deployment). Services, ingress,
  secrets, DB and storage are left as-is, so resume is fast and lossless.

Open: whether to also pause the ArgoCD Application or leave auto-sync on. Leaving auto-sync
on is simpler and correct here, because the suspended state (`replicas: 0`) is itself in Git
— ArgoCD keeping it in sync is exactly what we want.

### 4. Reset-on-change (the timer reset)

Any change to a deployment resets `last_deployed_at` to now and, if the deployment is
currently suspended, restores its replicas in the same commit. The two central code paths
that already process deployment changes are the hook points:

- `project_manager.upsert_deployment()` — API-driven image/config update or PR-preview create.
- `project_manager.process_project_from_git()` — project file change detected in Git.

After either applies a change, it updates the TTL state for that deployment. This gives the
desired behaviour: a new image push or any deployment edit keeps the environment alive for
another full TTL window; doing nothing lets it idle down.

### 5. State tracking

There is no per-deployment timestamp store today. Add a small table following the
`marked_for_deletion` schema style:

```
deployment_ttl_tracking
  project_name      varchar
  deployment_name   varchar
  cluster           varchar
  last_deployed_at  timestamptz   -- reset on every deployment change
  suspended         boolean       -- currently scaled to zero?
  suspended_at      timestamptz
  prev_replicas     int           -- replica count to restore on resume
  (primary key: project_name, deployment_name, cluster)
```

`expires_at` is derived (`last_deployed_at + ttl`), not stored, so changing the `ttl` value
takes effect immediately without a backfill.

### 6. Scheduling

There is no in-process scheduler in OPI; reconciliation runs only via an admin trigger.
TTL follows the same model:

- Add a TTL-reconcile endpoint (alongside the existing reconciliation / cleanup admin
  triggers), e.g. `POST /api/v2/admin/ttl/reconcile`, with a `dry-run` option.
- A small external `CronJob` calls it periodically (proposed: hourly). TTL granularity is
  hours/days, so hourly is plenty.
- This avoids new in-process scheduler infrastructure and reuses the dry-run-first safety
  pattern from `yaml-diff-driven-deletion.md`.

(If/when the planned auto-scale scheduler from `auto-scale-resources.md` lands, the TTL
reconcile can move into it — the endpoint stays as the manual/testing entry point either way.)

### 7. Resume / re-enable

A deployment comes back up when:
1. Its TTL is reset by a change (§4) — automatic, in the same flow, or
2. It is explicitly re-enabled via the API (a `:resume` / re-enable call), which restores
   `prev_replicas`, clears `suspended`, and resets `last_deployed_at`.

Resume = render the stored `prev_replicas` back into the manifest → commit → ArgoCD sync.

## What this replaces / keeps

| Piece | Now | After |
|---|---|---|
| Idle PR-env handling | none (stays fully sized) | scaled to zero after TTL |
| Apply path | project file/manifests + Git + ArgoCD | unchanged |
| Data (DB/bucket/PVC) | kept | kept (suspend never deletes) |
| Removal of *deleted* resources | `yaml-diff-driven-deletion` | unchanged (separate concern) |
| Right-sizing running pods | auto-tune / oom-watcher (VPA idea) | unchanged (complementary) |

## Dependencies / related

- `vpa-as-tuning-recommender.md` — complementary (sizing vs. lifetime); same root problem
  (idle PR envs wasting quota).
- `auto-resource-tuning.md` / `auto-scale-resources.md` — share the update→commit→sync apply
  path and the planned-scheduler discussion.
- `yaml-diff-driven-deletion.md` — the marked-for-deletion / grace-period / dry-run pattern
  this design mirrors (but suspend, not delete).
- `upsert-deployment-api.md` — the PR-preview entry point that gets the default TTL.

## Open questions

- **Default TTL value** for PR previews: `7d` proposed — confirm with the team. Robbert's
  prior practice was "review envs off after a week", which matches.
- **Quota accounting**: scaling to zero frees `requests`, but does the Capsule quota free
  immediately when pods are gone? (Expected yes — quota counts running pods — but verify on
  ODCN.)
- **User visibility**: surface suspended state + `expires_at` in the portal, and warn before
  suspend? At minimum the portal should show "suspended, click to resume".
- **Scale-to-zero vs. components with multiple deployments/replicas**: confirm `prev_replicas`
  capture is correct when a deployment was itself auto-tuned/auto-scaled between deploy and
  suspend (read the live/desired count at suspend time, not the original).
- **Stateful components**: are there workloads where `replicas: 0` is unsafe (e.g. a
  singleton holding a lease)? Suspend is double opt-in (`type: ephemeral` + `ttl`), so this is
  well bounded, but document it.
- **Mismarking a root as ephemeral**: guard 3 (canonical/domain-owning deployment) is the
  backstop — confirm the canonical-deployment detection is reliable (domain ownership and/or
  absence of `clone-from`) before relying on it.
- **Notification**: should an external trigger (chat/email) fire on suspend so PR authors know
  their env idled down?
```
