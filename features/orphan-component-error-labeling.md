# Orphan Component Error Labeling

## What it is

The project-details page shows a per-deployment status card with a live error
feed pulled from ArgoCD on every page load (resource tree + namespace events,
via `gather_deployment_errors` → `interpret_argocd_errors`). When a component is
removed from the project file but its Kubernetes resources have not yet been
pruned from the cluster, those leftover resources keep crashing and previously
surfaced as red "X problemen gevonden" errors — for components the user no
longer has in their project.

This feature labels such errors instead of hiding them: an error whose resource
belongs to a component no longer defined in the deployment is flagged as an
orphan leftover and rendered de-emphasised ("opruiming nog niet voltooid"),
separate from the real, actionable errors.

## How it works

Kubernetes workloads for a component are named `{deployment}-{component}` (see
`generate_unique_name` in `opi/utils/naming.py`); pods and replicasets carry
that name plus a hash. `interpret_argocd_errors` receives the list of components
currently in the deployment and flags any error entry whose full resource name:

1. starts with the `{deployment}-` prefix (i.e. it is component-scoped), **and**
2. matches no current component (`{deployment}-{component}` exactly, or with a
   trailing `-`).

The trailing-dash guard prevents `magazijn` from matching `magazijna`. Entries
that are **not** component-scoped (app-level: `SyncOperation`, conditions,
shared resources) never carry the prefix and are never flagged. Flagging runs
*before* resource names are simplified to bare component names, because the full
`{deployment}-{component}` name is needed for the match.

Flagged entries get `orphaned = "true"`. The deployment card splits the feed:

- **Real errors** → the existing red "X problemen gevonden" expandable block.
- **Orphan errors** → a muted grey block: "*N meldingen van verwijderde
  componenten — opruiming nog niet voltooid*".

### Fail-safes

- An **empty or absent** component list disables flagging entirely, so a data
  glitch can never hide genuine errors.
- App-level errors are always shown as real errors.
- Orphan errors are **de-emphasised, not hidden** — the signal that a leftover
  resource is still running (and crashing) is preserved.

## Scope

This is a **UI/labeling** change only. It does not prune the orphaned resources.
The deployment's ArgoCD health badge stays `Degraded` while an orphan pod
crashes — which is correct, because the application genuinely is unhealthy. The
underlying cleanup (why removed-component resources are not reliably pruned)
remains a separate reconciliation concern.

## Files

- `opi/services/event_interpreter.py` — `_is_orphaned_resource` helper +
  `component_names` parameter on `interpret_argocd_errors`.
- `opi/web/router.py` — passes the deployment's current `component_names`
  (`[c.reference for c in deployment.components]`) into the interpreter.
- `opi/templates/project-details/_argocd-deployment-card.html.j2` — splits the
  feed into real vs. orphan blocks.
- `tests/test_event_interpreter.py` — `TestOrphanedComponentFlagging`.

## Dependencies

None beyond the existing ArgoCD status path. Requires an OPI image rebuild and
rollout to take effect.
