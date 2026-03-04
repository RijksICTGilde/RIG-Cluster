# Known Issues

- Removing a key from a Kubernetes Secret does not trigger an ArgoCD sync. Related: https://github.com/argoproj/argo-cd/issues/24882

## Sandbox Setup

### GHCR egress limit causing ArgoCD repo server timeout

During setup, the ArgoCD repo server rollout can time out if GitHub Container Registry returns a `503 Egress is over the account limit` error when pulling `ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest`. This happens because the image uses the `latest` tag with `imagePullPolicy: Always`, so Kubernetes attempts a fresh pull even when the image is already cached on the node.

**Workaround:** Re-run `task sandbox:setup`. The image is typically cached on the Kind node from a previous attempt, and the rollout will succeed once the old pod finishes terminating. The setup is idempotent.

### Secrets overview files blocking re-runs

When `task sandbox:setup` fails partway through, the generated `secrets-overview-*.yaml` files remain in the project root. On the next run, the setup refuses to continue to avoid overwriting passwords you may not have saved yet.

**Workaround:** Delete the leftover overview files and re-run:

```bash
rm -f secrets-overview-*.yaml
task sandbox:setup
```

### ArgoCD operator CRD deletion timeout

The `prepare-argocd-operator` task uses `kubectl replace --force` to apply the ArgoCD operator, which deletes and recreates the CRD. When ArgoCD resources already exist in the cluster (e.g. from a previous partial setup), the CRD deletion blocks on finalizers — the ArgoCD CR has an `argoproj.io/finalizer` that can't be processed because the operator itself is being replaced. This creates a deadlock that hangs indefinitely or fails with `context deadline exceeded`.

**Workaround:** In a separate terminal, remove the finalizer to unblock the deletion:

```bash
kubectl patch argocd argocd -n rig-system --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]'
```

The setup will then continue automatically.
