# Skaffold Python Debugging Setup

## Goal
Enable breakpoint debugging in PyCharm/IntelliJ when running the application via Skaffold in Kubernetes.

## Steps

### 1. Use `skaffold debug` instead of `skaffold dev`
```bash
skaffold debug
```
This automatically injects `debugpy` into Python containers and exposes port 5678.

### 2. Configure uvicorn for single-worker mode during debug
- debugpy can only attach to one process
- Ensure uvicorn runs with `--workers 1` (or no `--workers` flag) when debugging
- Consider a Skaffold profile or environment variable to toggle this

### 3. Create PyCharm/IntelliJ Debug Configuration
1. **Run > Edit Configurations > + > Python Debug Server**
2. Host: `localhost`
3. Port: `5678`
4. Click **Debug**
5. Set breakpoints as normal

### 4. Optional: Configure port forwarding in `skaffold.yaml`
If port 5678 conflicts or needs explicit configuration:
```yaml
portForward:
  - resourceType: deployment
    resourceName: your-app
    port: 5678
    localPort: 5678
```

## Notes
- `skaffold debug` automatically disables liveness/readiness probes so Kubernetes won't restart the pod while paused on a breakpoint
- `--auto-sync` can be added alongside debug for file syncing
- No code changes needed in the application — Skaffold handles debugpy injection
