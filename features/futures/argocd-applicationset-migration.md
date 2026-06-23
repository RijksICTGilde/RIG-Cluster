# ArgoCD app-of-apps → ApplicationSet migration (non-destructive)

> **Status: PLANNED / not started.** Design complete, verified against live
> `odcn-production`. Implementation not begun. Pick up from "Next steps" below.
> Band-aid already shipped (see Context). All implementation MUST happen in a
> **separate git worktree** — the shared checkout is never touched.

## Context — why

New-deployment provisioning in prod takes **1–4 minutes** and intermittently
**fails falsely**. Root cause (verified live, not theory): the
`user-applications` app-of-apps umbrella has **no `manifest-generate-paths`
annotation**, so every refresh re-renders **all ~90 child subfolders** via the
`kustomize-sops-v1.0` CMP plugin (8–19 s repo-server renders, serialized). On a
new app OPI refreshes the umbrella and waits for the child Application to appear;
the umbrella render regularly exceeds OPI's create timeout.

Incident 2026-06-22: `otr-9i7-productie` provisioned correctly (namespace, SOPS
key, DB, MinIO, app all created) but the umbrella created the child app at
10:38:13, **5 s after** OPI gave up at 10:38:08 → task marked **failed at 267 s**
despite full success. (The logwatcher's "namespace never created / Forbidden
cascade" was a misdiagnosis: the Forbidden was transient RBAC-propagation lag
that self-healed on retry; the namespace is Active in prod — the triage likely
checked the sandbox context.)

**Band-aid already applied** (uncommitted, in current working tree):
`wait_for_application_created` timeout `120 → 360 s` at two sites in
`opi/manager/project_manager.py`. This migration is the durable fix.

## Goal

Replace the umbrella with an **ApplicationSet** so child Applications are
generated cheaply (template from params, no 90-folder CMP render), **without
pruning/deleting any of the 95 live Applications or their workloads** (93 carry
`resources-finalizer.argocd.argoproj.io` → deleting a child cascade-deletes its
live workloads).

## Verified current state (live odcn-production)

- ArgoCD operator-managed in `rig-prd-operations`; 1 application-controller, 2
  repo-servers, applicationset-controller **deployed but unused** (no webhook
  flags), 0 ApplicationSets.
- Umbrella `user-applications`: project `default`, source
  `github.com/RijksICTGilde/argo-applications.git` path `odcn-production`, plugin
  `kustomize-sops-v1.0` env `KUSTOMIZE_FOLDERS=subfolders`,
  `automated.prune=true, selfHeal=true`. No `manifest-generate-paths`.
- 95 child apps; 93 carry the finalizer; children have **no ownerReferences**
  (managed only via umbrella git render + a `tracking-id` annotation) — so an
  ApplicationSet can **adopt them by name** (apply = update, not recreate).
- Per project the umbrella renders THREE kinds into
  `argo-applications.git/odcn-production/{project}/`: child Application(s) (one
  per deployment), a per-project `AppProject` `{project}-{project}` (real
  isolation boundary; only 2 apps use `default`), and a SOPS-encrypted repo
  `Secret` `{project}-main-repo`.
- All 36 repo secrets share **one identical token** (verified by hash) for the
  same repo `github.com/RijksICTGilde/rig-cluster-application-test`; the
  `{project}@` userinfo in each repoURL is cosmetic → collapsible to ONE
  `repo-creds` template.
- Trigger reality: OPI's real trigger is the umbrella **`refresh` API call**;
  the GitHub webhook is unauthenticated/secondary; **appset webhook support is
  not enabled**. ApplicationSets have **no `refresh` API** (default re-scan ~3
  min) — so the trigger must be handled explicitly (see open sub-decision).

## Decisions locked in

- **Scope:** migrate **deployment AND infrastructure apps** in one cutover (the
  infra flow is a second umbrella consumer; partial migration can't retire it).
- **Rollout:** **canary then widen** — adopt `asses-k2n` (`productie` + `pr-350`)
  first, pass the gate, then all 95.
- **Config stays in Git** (params files in `argo-applications.git`).
- **Worktree isolation (HARD):** all implementation in a separate `git worktree`
  on a new branch; the shared checkout is never branch-switched/edited/deleted.
- **Sandbox first:** validate the whole cutover on `sandboxed-local` (3-env
  bootstrap) before `local`/`odcn-production`.

## Open sub-decision — trigger / "drop-in" webhook (decide before implementing)

- **(A) Enable ApplicationSet webhook support** — existing Git webhook
  regenerates the set instantly; config stays in Git; no OPI trigger code.
  Needs appset webhook enabled on argocd-server + secret + version check.
- **(B, recommended) OPI applies the child Application CR directly** after
  committing params (instant, deterministic, in OPI's control); the
  ApplicationSet adopts it and is steady-state owner/GC. Most faithful drop-in
  for OPI's current synchronous create+wait; slightly more OPI code.

## Target architecture

- **Applications → ApplicationSet, git `files` generator.** OPI writes a small
  `config.json` per deployment at `odcn-production/{project}/{deployment}/config.json`;
  one `ApplicationSet` (`goTemplate: true`,
  `files:[{path:"odcn-production/*/*/config.json"}]`) templates each child to
  **byte-match the current live spec** (name, namespace, `project` label,
  `sync-wave:1` + `manifest-generate-paths:.`, finalizer, `spec.project`=
  `{project}-{project}`, plugin+SOPS env, destination, full syncPolicy, retry,
  revisionHistoryLimit). Removes the per-project kustomization + CMP render =
  the speedup.
- **AppProjects → dedicated lightweight ArgoCD Application** rendering only
  `odcn-production/_appprojects/` (fast, low-churn; keeps the isolation boundary
  GitOps-managed). ApplicationSet can't emit AppProjects.
- **Repo creds → one `repo-creds` template** in bootstrap (3 overlays),
  replacing 36 per-project secrets. **Caveat:** `repo-creds` prefix-match does
  NOT tolerate `{project}@` userinfo → ApplicationSet must emit a **normalized
  repoURL (no userinfo)**, and the cred template MUST exist/reconcile **before**
  any repoURL loses its userinfo.

## Non-destructive cutover ordering (reversible; sandbox first)

1. **Snapshot** all app/AppProject specs + tracking-ids (out-of-band). Baseline:
   95 apps.
2. **Disarm umbrella `prune`+`selfHeal`** (live patch + bootstrap, 3 overlays).
   Nothing can prune a child after this. *Rollback: revert.*
3. **Create org `repo-creds` template** (bootstrap, 3 overlays); verify ArgoCD
   resolves creds for the bare URL. 36 per-project secrets stay, coexist.
   *Rollback: delete secret.*
4. **Stand up dedicated AppProject Application**; populate `_appprojects/`
   verbatim; sync `prune=false`; verify AppProject count unchanged.
   *Rollback: delete app `--cascade=orphan`.*
5. **Apply ApplicationSet adopt-only** — `applicationsSync: create-update`
   (never deletes) + `preserveResourcesOnDeletion: true`. **Canary `asses-k2n`
   first.** Gate (all must hold): count still 95; each adopted spec identical to
   snapshot except bare repoURL + reassigned tracking-id; no OutOfSync/Progressing
   storm; pods' restartCount/age unchanged; appset `ResourcesUpToDate=True`.
   *Rollback: `kubectl delete applicationset user-applications --cascade=orphan`
   — leaves all apps live.*
6. **Widen** params to all; re-run gate across 95.
7. **Retire umbrella LAST:** remove from 3 overlays, then `kubectl delete
   application user-applications --cascade=orphan` (**mandatory** — plain delete
   triggers the finalizer cascade on 93 children → destroys workloads). Verify
   umbrella gone, 95 Synced/Healthy, appset sole owner, 3 workloads unchanged.

## OPI code changes (in the worktree; minimal)

- `opi/manager/argo_manager.py`: `create_repository_secrets` → no-op for
  shared-token HTTPS; `create_app_projects` → write to `_appprojects/`;
  `create_applications` → write `config.json` with **normalized repoURL** (stop
  calling `make_argocd_repository_url_unique`); `create_kustomization_files` →
  drop/repoint. Dead: `generate_application_manifest`,
  `make_argocd_repository_url_unique` (app/secret), `manifests/argocd-application.yaml.jinja`,
  `manifests/argo-repository-https.yaml.jinja`.
- `opi/manager/project_manager.py` / `opi/connectors/argo.py`: remove umbrella
  refresh + wait-for-created on create (replace per trigger option A/B);
  repurpose `default_app_name`; migrate the infra flow off the umbrella.
- `opi/manager/delete_project_manager.py`: the 4 umbrella-refresh delete sites →
  remove `config.json` + explicit `kubectl delete application <child>` (finalizer
  cascade IS desired on delete) + remove AppProject from `_appprojects/`.

## Key risks → neutralization

- **Finalizer cascade (93):** disarm prune FIRST + retire with `--cascade=orphan`.
- **Appset deletes on missing generator item:** `create-update` +
  `preserveResourcesOnDeletion: true`.
- **repoURL/cred mismatch:** cred template before repoURL normalization;
  non-destructive failure but gated.
- **tracking-id / repoURL change re-sync:** validate on sandbox pods aren't
  restarted.
- **3-env bootstrap:** parallel changes; set appset `template.metadata.namespace`
  explicitly (kustomize namespace xform won't rewrite it). Sandbox first.

## Verification (sandbox before prod)

1. Adoption is a no-op (no workload restart, no OutOfSync storm).
2. `repo-creds` template serves bare URL; per-project secrets removable.
3. repoURL normalization doesn't churn workloads.
4. tracking-id reassignment benign.
5. `kubectl delete application user-applications --cascade=orphan` leaves all
   children + workloads live.
6. Project-delete still cascades correctly under the new model.
7. New `config.json` → live app **within seconds** (vs today's 1–4 min).

## Next steps (resume here)

1. Decide the trigger sub-decision (A vs B; recommended B).
2. Create a git worktree on a new branch (never touch the shared checkout).
3. Build the ApplicationSet + `repo-creds` + AppProject-app manifests for
   `sandboxed-local`; dry-run adoption against a live child spec diff.
4. Run the full cutover on sandbox; validate the 7 verification points.
5. Replicate to `local` + `odcn-production` overlays; canary `asses-k2n`; widen.
6. Commit the OPI code changes; rebuild + roll out OPI; retire the umbrella.

## Source material

Full planning detail (incl. the verified kubectl evidence and the ApplicationSet
template skeleton) was captured during the 2026-06-22/23 session. Key files:
`bootstrap/rig-system/kustomize/overlays/{odcn-production,sandboxed-local,local}/argocd-application-user-applications.yaml`,
`bootstrap/rig-system/kustomize/secrets/templates/argocd-repo-*-secret.yaml`,
`bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml`,
`operations-manager/python/opi/manager/argo_manager.py`,
`operations-manager/python/manifests/argocd-application.yaml.jinja`.
