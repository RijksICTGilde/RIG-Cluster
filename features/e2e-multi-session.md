# E2E Multi-Session Coordination

## What It Is

A protocol for multiple Claude sessions (dclaude) to coordinate E2E testing against a shared sandbox cluster. Feature sessions develop in parallel, a coordinator session manages merges and deployments, and feature sessions run Playwright tests to validate their changes.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  dclaude sessions                                             │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐             │
│  │ feature-a│  │ feature-b│  │  coordinator   │             │
│  └────┬─────┘  └────┬─────┘  └───────┬────────┘             │
│       │              │                │                       │
│       └──────────────┴────────────────┘                       │
│                      │ inter-session messaging                │
│  ┌───────────────────▼────────────────────────────────┐      │
│  │  Sandbox Cluster (Kind + Caddy)                     │      │
│  │  OPI synced via Skaffold hot-reload                 │      │
│  └─────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────┘
```

## Coordination Protocol

### 1. Feature Session Finishes Coding

When a feature session completes its implementation:

```bash
send-message coordinator "Feature X implementation complete on branch claude/feature-x. Ready for merge and E2E testing."
```

### 2. Coordinator Merges and Deploys

The coordinator session:
1. Merges the feature branch into the deploy branch
2. Waits for Skaffold to sync and the OPI pod to become ready
3. Verifies the pod is running:

```bash
kubectl wait --for=condition=ready pod -l app=operations-manager -n rig-system --timeout=120s
```

### 3. Coordinator Signals Feature Session

```bash
send-message feature-x "Merge deployed. OPI pod ready. Run your E2E tests now."
```

### 4. Feature Session Runs Tests

```bash
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
  uv run pytest tests/e2e/ -m "e2e and sandbox" -v --timeout=300
```

### 5. Feature Session Reports Results

```bash
send-message coordinator "E2E tests passed (5/5). Feature X validated."
# or
send-message coordinator "E2E tests failed: test_wizard_minimal_project — timeout on step 3. See logs." --priority high
```

## Testing Mutex

Only one session should run sandbox E2E tests at a time to avoid project name collisions and interference. The coordinator manages this:

1. Feature session requests test slot
2. Coordinator grants slot (or queues request)
3. Feature session runs tests
4. Feature session releases slot

This is currently a messaging convention, not a technical lock.

## Quick Reference

| Action | Command |
|---|---|
| Run local E2E tests | `task test-e2e` |
| Run sandbox E2E tests | `task test-e2e-sandbox` |
| Check OPI pod status | `kubectl get pods -n rig-system -l app=operations-manager` |
| View OPI logs | `kubectl logs -n rig-system deployment/operations-manager -f` |
| Message coordinator | `send-message coordinator "message"` |

## Dependencies

- Sandbox cluster running (`task sandbox:setup`)
- Caddy configured with sandbox domain routing (see [E2E Testing](e2e-testing.md))
- Skaffold hot-reload active (`task sandbox:skaffold-dev`)
- `send-message` CLI available in dclaude sessions
