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
              Refresh completes -> schedules another OOM check (attempt 2/3)
              ... until no OOM or max attempts (3) reached
```

### Key properties

- The deploy/refresh task completes immediately - the OOM check is a detached background coroutine
- OOM detection uses **kubectl** (pod container status `lastState.terminated.reason == "OOMKilled"`)
- The fix: increase memory in YAML via the existing resource tuning service, then git commit and trigger refresh
- Natural recursion: each refresh schedules its own check, capped at 3 attempts
- After 3 rounds of 1.5x increases (e.g., 256Mi -> 384Mi -> 576Mi -> 864Mi), manual intervention is needed

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
| `opi/services/oom_watcher.py` | Fire-and-forget OOM check logic |
| `opi/services/resource_tuning_service.py` | Extracted tune logic (shared by HTTP endpoint and watcher) |
| `opi/api/resource_router.py` | HTTP endpoint (thin wrapper over service) |
| `opi/core/config.py` | OOM watcher settings |
| `opi/core/task_handlers_operations.py` | Refresh handler integration |
| `opi/core/task_handlers_project.py` | Create/upsert handler integration |

### Attempt tracking

The `oom_watch_attempt` field in the task payload tracks which attempt number the current check is on. When the tune service triggers reprocessing, the refresh handler reads this field and passes `attempt + 1` to the next `schedule_oom_check` call.

## Troubleshooting

### Watcher not triggering

- Check `OOM_WATCHER_ENABLED` is `True`
- Look for log messages: `OOM watcher: scheduled check for ...`
- The delay is 2 minutes by default - check after the delay period

### OOM detected but not fixed

- Check logs for `OOM watcher: auto-tune applied N change(s)`
- If max attempts reached, you'll see: `OOM watcher: max attempts (3) reached ... manual intervention required`
- Verify the metrics backend (Prometheus) is available - the tune service needs it to compute recommendations

### kubectl connectivity

- The watcher uses kubectl to query pod statuses
- If kubectl is not connected, you'll see: `kubectl not connected, cannot check OOM kills`
- The watcher degrades gracefully - it won't crash, just logs warnings

## Dependencies

- [Auto Resource Tuning](auto-resource-tuning.md) - the underlying tune logic
- kubectl connectivity to the target cluster
- Prometheus/metrics backend for computing memory recommendations
