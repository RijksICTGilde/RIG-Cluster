# ArgoCD Render/Compare Error Surfacing

## What It Is

When ArgoCD cannot **generate or compare** a deployment's manifests - a broken kustomization,
a duplicate resource identity, an invalid manifest, or any CMP (Config Management Plugin)
render failure - it sets `sync.status = Unknown` and records the cause in
`status.conditions[]` as a `ComparisonError`. It does **not** start a sync operation, so there
is no `operationState.phase` to key off.

Previously OPI read only `operationState.phase` and `health.status`, so a render error was
invisible: the deploy ran to its 300s timeout and reported a generic "time-out, check the
component logs" for a component that was never rendered, and the project page kept showing the
last known health. The real message lived only in the ArgoCD UI.

This feature makes that error visible in three places.

## How It Works

### 1. During the deploy (fail fast)

- `ArgoManager.wait_for_application_synced` and `wait_for_infrastructure_ready` read
  `status.conditions[]` on a fresh status (respecting the `refreshed_after` guard) and raise a
  `RuntimeError` with the condition message - which carries the plugin stderr - the moment a
  terminal condition (`ComparisonError`, `InvalidSpecError`, `SyncError`, `UnknownError`)
  appears. The deploy fails immediately with the real cause instead of timing out.
- After pushing a deployment's manifests, OPI actively fetches the render via
  `ArgoConnector.get_application_manifests` (the API behind `argocd app manifests`). A broken
  kustomization returns the generation error there, so the deploy fails fast. This is also the
  only channel that covers helm/helmfile deployments, which render only inside the CMP. A
  transport/auth error (network, 401) is logged and the deploy continues to the normal wait.

### 2. In the project status card

`_fetch_argocd_deployment_status` reads the cheap app-level conditions
(`deployment_diagnostics.conditions_to_errors`) unconditionally - even when health still reads
`Healthy` from the last good reconciliation - so a `ComparisonError` is no longer filtered out
behind the health guard. A `ComparisonError` is rendered with a readable heading,
"Configuratiefout (kustomize CMP)", and the raw kustomize/CMP message underneath. The sync
badge shows `Unknown` in red instead of neutral grey.

### 3. As a pre-commit failsafe

`create_kustomization_files` scans the on-disk manifests and raises before commit/push if two
files declare the same `(apiVersion, kind, namespace, name)` - the most common render failure
(e.g. a marked-for-deletion PVC left next to a recreated one). This catches the error before
it ever reaches ArgoCD.

### 4. Clearer CMP logs

The kustomize build in the CMP script (`bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml`)
ends a failure with a single, final `KUSTOMIZE BUILD FAILED in <folder>: <stderr>` line, so
the real error survives ArgoCD's truncation of long plugin output and is easy to find. The
`.cmp-env` contents (which hold secrets) are no longer dumped to the logs.

## Files

| File | Purpose |
|------|---------|
| `opi/manager/argo_manager.py` | `terminal_condition_message`; conditions read in the wait loops |
| `opi/connectors/argo.py` | `get_application_manifests`; render-error WARNING on refresh |
| `opi/manager/project_manager.py` | Active render fetch after push; `_looks_like_render_failure` |
| `opi/services/deployment_diagnostics.py` | `conditions_to_errors` (cheap, always-run) |
| `opi/services/event_interpreter.py` | Readable `ComparisonError` heading |
| `opi/web/router.py` | Conditions read unconditionally in the status card |
| `opi/templates/project-details/_argocd-deployment-card.html.j2` | Red `Unknown` sync badge |
| `opi/generation/manifests.py` | Duplicate resource-identity failsafe |
| `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml` | Final kustomize-error line on stderr |
