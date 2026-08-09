# Known Issues

- Removing a key from a Kubernetes Secret does not trigger an ArgoCD sync. Related: https://github.com/argoproj/argo-cd/issues/24882

## Het standaardschema kapt stil af (niet gerepareerd)

Genoteerd bij RC-59, bewust niet daar gerepareerd.

`generate_database_schema` maakt de naam van het standaardschema als
`{project}_{deployment}` en kapt die met een kale `name[:63]` af, zonder hash of andere
onderscheidende staart. Twee deployments in hetzelfde project met lange namen die pas na
teken 63 uit elkaar lopen, krijgen dus **hetzelfde standaardschema** en zitten ongemerkt in
elkaars data.

RC-17 heeft dit voor *extra* schema's juist voorkomen: `generate_extra_database_schema`
gooit een `ValueError` in plaats van af te kappen, en sinds RC-59 wordt die controle bij
elke opslag gedraaid, dus ook als er een deployment bijkomt. De standaardweg is daar nooit
in meegenomen.

Waarom het hier blijft staan: een naamgevingsregel wijzigen raakt **bestaande databases**.
Elke oplossing (hard falen, een hash toevoegen, de deploymentnaam begrenzen) is een migratie
met een eigen vraag over wat er met de al aangemaakte schema's gebeurt. Dat hoort een eigen
taak te zijn, niet een bijvangst van een API-uitbreiding.

## Sandbox Setup

### Forgejo pod restart causing sandbox:sync failure (fixed)

The `sandbox:sync` task could fail with `unable to forward port because pod is not running. Current status=Pending` when the Forgejo pod restarted between `sandbox:init-forgejo` and `sandbox:sync`. The init step (creating admin user and 4 repositories) can cause memory pressure or liveness probe failure, causing the pod to restart. The port-forward fallback in `sandbox:sync` had no wait logic, so it would immediately fail if the pod wasn't Running.

**Fix:** Added `kubectl wait --for=condition=Ready` before the port-forward attempt, giving the pod up to 120s to become Ready again.

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

The `prepare-argocd-operator` task uses `kubectl replace --force` to apply the ArgoCD operator, which deletes and recreates the CRD. When ArgoCD resources already exist in the cluster (e.g. from a previous partial setup), the CRD deletion blocks on finalizers - the ArgoCD CR has an `argoproj.io/finalizer` that can't be processed because the operator itself is being replaced. This creates a deadlock that hangs indefinitely or fails with `context deadline exceeded`.

**Workaround:** In a separate terminal, remove the finalizer to unblock the deletion:

```bash
kubectl patch argocd argocd -n rig-system --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]'
```

The setup will then continue automatically.
