# Avoid Unnecessary Reprocessing and SOPS Churn

## Problem

Every `process_project` run regenerates ALL resources (manifests, secrets, ArgoCD configs) and commits them to git — even when nothing actually changed. SOPS encryption uses random nonces, so re-encrypting identical plaintext produces different ciphertext every time. This means git always sees a diff, commits always happen, and ArgoCD always has something to reconcile.

The result:
- Unnecessary git commits on every reprocess
- ArgoCD reconciliation delays for changes that don't exist
- `user-applications` refresh triggers a heavy sync of 100+ resources
- Operations that should take seconds (resource tuning, image updates) take minutes

## Root causes

### 1. SOPS nonce randomness
SOPS uses a random nonce for each encryption operation. Even if the plaintext secret hasn't changed, the encrypted output differs. Since `create_argocd_resources` re-encrypts repository secrets every time, git always has a diff.

### 2. No change detection before commit
`create_argocd_resources` always regenerates and commits AppProject, Application, repository secret, and kustomization files — without checking if the content actually changed.

### 3. Full pipeline for partial changes
`process_project` runs the entire pipeline (namespaces, databases, keycloak, minio, redis, manifests, ArgoCD resources, bootstrap) even for operations that only affect one aspect (e.g. resource limits).

## Possible approaches

### SOPS: decrypt-before-encrypt comparison
Before re-encrypting a secret file, decrypt the existing file and compare plaintext. Only re-encrypt if the plaintext actually changed. This avoids the nonce-driven false diffs.

**Trade-off**: Adds a decrypt step per file, but saves unnecessary commits and ArgoCD reconciliation. Could use checksums of plaintext for fast comparison.

### ArgoCD resources: skip when unchanged
For non-SOPS files (AppProject, Application YAML, kustomization), compare the generated content against the existing file on disk before writing. Only write if different.

**Trade-off**: Simple file comparison, but doesn't help with SOPS files.

### Selective pipeline execution
Instead of always running the full pipeline, determine which steps are needed based on what changed. For example:
- Resource tuning → only regenerate manifests
- Image update → only regenerate manifests for that deployment
- New service added → run service managers + manifests
- New deployment → full pipeline including ArgoCD resources

This requires understanding the "intent" of each reprocessing call — potentially via an enum or flags indicating what changed.

### Git-level deduplication
Before committing, check `git diff --stat` — if no files actually changed (or only SOPS nonce changes), skip the commit entirely.

**Trade-off**: Doesn't work for SOPS files since they always diff. Would need the decrypt-before-encrypt approach first.

## Impact

High — affects every project reprocessing operation. The `user-applications` refresh alone adds 30-120 seconds to operations that would otherwise complete in seconds. Multiply by the number of tune/update operations per day across all projects.

## Dependencies

- SOPS encryption workflow in `opi/utils/sops.py`
- ArgoCD resource generation in `opi/manager/argo_manager.py`
- Git connector commit logic in `opi/connectors/git.py`
- `process_project` pipeline in `opi/manager/project_manager.py`
