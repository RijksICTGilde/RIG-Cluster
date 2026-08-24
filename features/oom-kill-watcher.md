# OOM Kill Watcher - Fire-and-Forget Auto-Tune

## What it is

The OOM Kill Watcher is a fire-and-forget background mechanism that automatically detects and recovers from Out-of-Memory (OOM) kills after deployments. When a deploy or refresh completes, a delayed check runs in the background. If OOM kills are detected, the watcher automatically increases memory limits and triggers reprocessing - no manual intervention needed.

## How it works

```
Deploy/Refresh completes
        |
        | asyncio.create_task() - fire and forget
        v
   [sleep 2 min]
        |
        v
   Query kubectl: any OOM kills for this deployment's pods?
        |
   NO --+--> Done. Log "no OOM detected" and exit.
        |
   YES -+--> Call tune service:
             1. Increase memory limits (1.5x current) in project YAML
             2. Commit to git
             3. Trigger refresh on the deployment
                    |
                    v
              Refresh completes -> schedules another OOM check (tune cycle 2/3)
              ... until no OOM, or one of the brakes below closes
```

### Key properties

- The deploy/refresh task completes immediately - the OOM check is a detached background coroutine
- OOM detection uses **kubectl** (pod container status `lastState.terminated.reason == "OOMKilled"`)
- The fix: increase memory in YAML via the existing resource tuning service, then git commit and trigger refresh
- Natural recursion: each refresh schedules its own check, capped at 3 tune cycles per deployment
- After 3 rounds of 1.5x increases (e.g., 256Mi -> 384Mi -> 576Mi -> 864Mi), manual intervention is needed

## The brakes

Four things bound the escalation. Each is independent: any one of them would have
stopped the 24 August 2026 incident, where `asses-k2n/pr-494` walked from 45Mi to the
4096Mi cluster ceiling in nine rounds.

| Brake | What it bounds | Where |
|---|---|---|
| The tune budget | 3 tune cycles per deployment | `_oom_tune_attempts` in `oom_watcher.py` |
| The pod-generation lock | one tune per pod generation | `_last_tuned_pod_template_hash` in `oom_watcher.py` |
| The growth ceiling | 8x the declared limit | `max_growth_factor` in the resource-tuning service config |
| The cluster ceiling | `get_max_memory_limit_mi` (4096Mi on odcn-production) | `cluster_config.py` |

**The tune budget is per deployment, not per round.** One counter, keyed
`"{project}/{deployment}"`, shared by the inline path and the fire-and-forget path, read
live on every check. It deliberately survives a round: every committed tune queues a
`refresh_deployment` task, and that task schedules a new check starting at `attempt=1`.
A counter that reset there would reset the very brake it is. Only an explicit
`reset_inline_oom_attempts` clears it, and only for a user action: a deploy, an upsert, a
manual refresh, an image bump. The automated refresh a tune queues for itself carries
`automated_remediation: True` precisely so it can be told apart.

**The pod-generation lock.** A tune only means something once it is running. Both paths
record the `pod-template-hash` of the pod the OOM was observed on; a later detection on
that same hash is not new evidence, because the previous increase has not rolled out yet.
(The existing superseded-generation filter cannot catch this: at that moment the pod still
IS the current generation.) When the hash cannot be determined the OOM counts as fresh -
blocking there would silence the auto-tune the moment kubectl hiccups.

**The growth ceiling** is described in [Auto Resource Tuning](auto-resource-tuning.md).

## Configuration

Three settings in `opi/core/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `OOM_WATCHER_ENABLED` | `True` | Enable/disable the watcher globally |
| `OOM_WATCHER_DELAY_SECONDS` | `120` | Seconds to wait after deploy before checking |
| `OOM_WATCHER_MAX_ATTEMPTS` | `3` | Maximum tune cycles per deployment |

Set via environment variables:

```bash
OOM_WATCHER_ENABLED=false     # Disable
OOM_WATCHER_DELAY_SECONDS=60  # Check sooner
OOM_WATCHER_MAX_ATTEMPTS=5    # More retries
```

## Where it triggers

The OOM watcher is scheduled at the end of:

1. **`handle_refresh_deployment`** - after a deployment refresh completes successfully
2. **`handle_upsert_deployment`** - after a new/updated deployment is processed successfully
3. **`handle_create_project`** - after project creation, for each deployment in the project

## Architecture

### Files

| File | Purpose |
|------|---------|
| `opi/services/oom_watcher.py` | Fire-and-forget OOM check logic (the OBSERVING) |
| `opi/services/catalog/deployment_health/` | The `deployment-health` system service: the JUDGEMENT over what is observed |
| `opi/services/deployment_state.py` | What the other services report about the deployment, weighed before judging |
| `opi/services/resource_tuning_service.py` | Extracted tune logic (shared by HTTP endpoint and watcher) |
| `opi/api/resource_router.py` | HTTP endpoint (thin wrapper over service) |
| `opi/core/config.py` | OOM watcher settings |
| `opi/core/task_handlers_operations.py` | Refresh handler integration |
| `opi/core/task_handlers_project.py` | Create/upsert handler integration |
| `opi/services/catalog/resource_tuning/config.py` | `max_growth_factor`: the growth ceiling |
| `scripts/oom_growth_report.py` | Read-only report on components an earlier, unbounded escalation left above the ceiling (`PROJECTS=... task oom-growth-report`) |

### Attempt tracking

The tune budget lives in the module-level `_oom_tune_attempts` dict in `oom_watcher.py`,
keyed `"{project}/{deployment}"`, and both paths gate on it (see "The brakes" above). The
`oom_watch_attempt` field in the task payload and the `attempt` parameter of
`schedule_oom_check` still exist, but they only bound the chain of SCHEDULED checks and
appear in the log lines - they no longer decide whether tuning is still allowed. They
cannot: every automated refresh starts a new chain at `attempt=1`.

## Troubleshooting

### Watcher not triggering

- Check `OOM_WATCHER_ENABLED` is `True`
- Look for log messages: `OOM watcher: scheduled check for ...`
- The delay is 2 minutes by default - check after the delay period

### OOM detected but not fixed

- Check logs for `Health watcher: auto-tune committed changes` (the tune now runs through the generic after-sync hook scan; see auto-resource-tuning.md)
- If the tune budget is spent, you'll see: `Health watcher: OOM tune budget (3 cycles) spent for ...` (background path) or `Health check: max OOM tune attempts (3) reached for ...` (inline path)
- If the growth ceiling refuses the increase, the message names the declared limit, the current limit and the factor, and asks for manual intervention - a limit WAS computed, it was refused
- If a detection lands on the generation a previous tune already answered: `... is on pod generation <hash>, the same one the previous tune answered - waiting for that increase to roll out`
- Verify the metrics backend (Prometheus) is available - the tune service needs it to compute recommendations

### kubectl connectivity

- The watcher uses kubectl to query pod statuses
- If kubectl is not connected, you'll see: `kubectl not connected, cannot check OOM kills`
- The watcher degrades gracefully - it won't crash, just logs warnings

## Dependencies

- [Diensten die elkaars toestand kennen](deployment-state-and-health.md) - the judgement
  lives in the `deployment-health` system service, which weighs what other services
  report (a sleeping deployment is meant to have no pods)
- [Auto Resource Tuning](auto-resource-tuning.md) - the underlying tune logic
- kubectl connectivity to the target cluster
- Prometheus/metrics backend for computing memory recommendations
