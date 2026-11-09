# Uitrollen

Hoe een nieuwe versie van de operations-manager (OPI) of van de CMP-sidecar van ArgoCD op een cluster komt: welke taken, in welke volgorde, en waar je op let. Voor mensen en voor agents. De achtergrond van het tagformaat en het pinnen staat in [features/image-versioning-calver.md](../../features/image-versioning-calver.md); de `staging`-branch in [features/zad-staging.md](../../features/zad-staging.md#een-release-eerst-op-staging).

## De clusters

| Cluster | `CLUSTER_TYPE` | kubectl-context | OPI-namespace | Volgt branch |
|---|---|---|---|---|
| Sandbox | `sandboxed-local` | `kind-rig-sandbox` | `rig-system` | de Forgejo in het cluster, gevuld met `task sandbox:sync` |
| Staging | `odcn-staging` | `odcn-rig-production` | `rig-staging-operations` | `staging` |
| Productie | `odcn-production` | `odcn-rig-production` | `rig-prd-operations` | `main` |

Staging en productie draaien op hetzelfde fysieke ODCN-cluster, in een andere tenant, met dezelfde context. Het verschil zit in `CLUSTER_TYPE`: dat kiest `.env-taskfile-<cluster>`, en daarmee de namespace, de AGE-sleutel en de secrets-repo.

## Spelregels

- **Bouw en deploy vanuit `main` in de gewone checkout** (`~/IdeaProjects/RIG-Cluster`), schoon en gepusht. Geen worktree of clone voor een OPI-release: dan klopt het branchlabel in de voettekst niet en moet je sleutels kopiëren. Loopt de checkout achter of is hij dirty, zoek dan eerst uit waarom.
- **Geef altijd `CLUSTER_TYPE=<cluster>` mee.** De taken weigeren als de geladen configuratie bij een ander cluster hoort.
- **Zet de kubectl-context vast.** `deploy-operations-manager` en `bootstrap-argo-system` gebruiken de huidige context, en de gedeelde `~/.kube/config` kan door een andere sessie van context wisselen. Werk met een eigen kopie, en maak die aan het begin van elke reeks opnieuw, want het token in de kubeconfig roteert:

  ```bash
  install -m 600 ~/.kube/config /tmp/kubeconfig-odcn
  KUBECONFIG=/tmp/kubeconfig-odcn kubectl config use-context odcn-rig-production
  export KUBECONFIG=/tmp/kubeconfig-odcn
  ```

  Doe `use-context` nooit op de gedeelde `~/.kube/config`.
- **Productie gaat altijd eerst langs staging**, en een uitrol naar productie is een besluit van een mens.
- **De publicatietaken vragen interactief om bevestiging** (`Continue with publishing? [y/N]`). Een agent beantwoordt die alleen als er om de uitrol gevraagd is, met `printf 'y\n' | task ...`.

## De operations-manager

### 1. Publiceren en pinnen

```bash
CLUSTER_TYPE=odcn-staging OVERLAY=odcn-staging task publish-operations-manager
```

Bouwt `linux/amd64` en `linux/arm64`, pusht de CalVer-tag (plus `latest`) naar GHCR en pint de overlay van `OVERLAY` (standaard `odcn-production`) op tag plus digest. Alleen die ene overlay verandert; elk cluster heeft zijn eigen OPI-pin.

### 2. De env-secret nalopen

`publish-operations-manager` en `deploy-operations-manager` versleutelen allebei de env-secret opnieuw: voor staging in `../zad-staging-secrets`, voor productie in RIG-Cluster zelf (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml`). Loop dit dus na elke publish en na elke deploy na. SOPS versleutelt dan opnieuw, ook als de inhoud gelijk is, en een dirty secrets-repo laat de volgende publish of deploy weigeren. Vergelijk de ontsleutelde inhoud met `HEAD`: is die gelijk, zet het bestand dan terug met `git checkout`. Verschilt hij, dan is er echt iets veranderd en hoort dat in een eigen commit in die repo.

### 3. De pin committen en deployen

```bash
git add bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-staging/patches/deployment.yaml
git commit -m "deploy: operations-manager <tag> naar staging"
CLUSTER_TYPE=odcn-staging task deploy-operations-manager
```

`deploy-operations-manager` past de overlay toe met kubectl; verandert de pod-template, dan rolt Kubernetes zelf uit.

### 4. Controleren

```bash
kubectl -n rig-staging-operations rollout status deploy/operations-manager
kubectl -n rig-staging-operations get deploy operations-manager -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl -n rig-staging-operations logs deploy/operations-manager --since=5m | grep " - ERROR - "
curl -s -o /dev/null -w "%{http_code}\n" https://zad.staging.rijksapp.dev/health
```

Voor productie: `rig-prd-operations` en `https://zad.rijksapp.nl/health`.

## De CMP-sidecar van ArgoCD

De sidecar (`rig-cmp-argo-kustomize-sops`) rendert elke Application op het cluster. Hij is daarmee de meest ingrijpende image om te vervangen: test hem eerst in de sandbox.

### De pin is gedeeld

Staging en productie gebruiken dezelfde pin, in de component `cmp-odcn` (RC-359, bewaakt door `tests/test_cmp_inrichting_per_odcn_cluster.py`). Een eigen pin in een overlay zet die toets rood, met reden: zo liep staging eerder stil uit elkaar. `pin-cmp-image` zet de pin op alle drie de plekken: `components/cmp-odcn`, `components/sops-plugin` (sandbox) en de ArgoCD-CR in de productie-overlay.

Wil je de sidecar alleen op staging, dan gaat de pin naar de `staging`-branch en nog niet naar `main`. De ArgoCD-CR wordt niet door ArgoCD gesynct maar door `bootstrap-argo-system` toegepast, dus productie verandert pas als iemand die taak voor productie draait.

### 1. Testen in de sandbox

Bouw het image lokaal, laad het in kind en zet het tijdelijk op de ArgoCD-CR van de sandbox:

```bash
docker build -t ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:test images/cmp-kustomize-sops
kind load docker-image ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:test --name rig-sandbox
kubectl --context kind-rig-sandbox -n rig-system get argocd argocd -o yaml > /tmp/argocd-cr-backup.yaml
kubectl --context kind-rig-sandbox -n rig-system patch argocd argocd --type=json -p '[
  {"op":"replace","path":"/spec/repo/sidecarContainers/0/image","value":"ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:test"},
  {"op":"replace","path":"/spec/repo/sidecarContainers/1/image","value":"ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:test"}]'
```

Wacht tot de operator de repo-server heeft uitgerold, geef elke Application een hard refresh (annotatie `argocd.argoproj.io/refresh=hard`) en kijk of ze Synced/Healthy blijven en of de sidecars `Plugin command successful` loggen. Zet daarna de image uit de backup terug.

### 2. Publiceren

```bash
task publish-cmp-kustomize-sops
```

Weigert een `-dirty`-tag. Pint daarna op alle drie de plekken in de checkout waar je hem draait. Moet de pin nog niet op `main` landen, draai dan alleen de build-stap en pin op een release-branch (hieronder):

```bash
TAG=$(scripts/calver-tag.sh images/cmp-kustomize-sops)
task docker-build-and-push BUILD_CONTEXT=. DOCKERFILE_PATH=images/cmp-kustomize-sops/Dockerfile \
  IMAGE_NAME=rig-cmp-argo-kustomize-sops REGISTRY_IMAGE=ghcr.io/minbzk/base-images IMAGE_TAG=$TAG \
  EXTRA_TAG=latest BUILDER_NAME=operations-manager-builder PLATFORMS=linux/amd64,linux/arm64
```

### 3. Pinnen op een release-branch voor staging

```bash
git worktree add -b release/staging-<datum> ../RIG-Cluster-release-staging main
cd ../RIG-Cluster-release-staging
task pin-cmp-image IMAGE_TAG=<tag>
git commit -am "deploy: CMP-sidecar <tag>"
```

Hoe `staging` daarna naar die commit gaat staat in [features/zad-staging.md](../../features/zad-staging.md#een-release-eerst-op-staging): fast-forward, nooit een eigen commit op `staging`, en dezelfde commit gaat later ongewijzigd naar `main`.

### 4. Eerst het verschil, dan toepassen

Render de overlay en vergelijk met wat er draait. Verwacht alleen de twee sidecar-images:

```bash
SOPS_AGE_KEY="$(sed -n '3p' ~/IdeaProjects/RIG-Cluster/security/odcn-staging-key.txt)" \
  kustomize build --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone \
  bootstrap/rig-system/kustomize/overlays/odcn-staging > /tmp/staging.yaml
kubectl diff -f /tmp/staging.yaml
```

Dan toepassen:

```bash
CLUSTER_TYPE=odcn-staging task bootstrap-argo-system KEY_FILE=$HOME/IdeaProjects/RIG-Cluster/security/odcn-staging-key.txt
```

In een worktree is `KEY_FILE` verplicht: de `.env-taskfile-*` staan in git met een relatief sleutelpad, en de sleutel zelf staat alleen in de gewone checkout. Zonder `KEY_FILE` past de taak de overlay wel toe maar faalt hij daarna op de repo-secrets, halverwege.

### 5. Controleren

Beide repo-server-pods draaien de nieuwe image, elke Application is na een hard refresh Synced/Healthy, en de sidecars loggen geen `"level":"error"`:

```bash
kubectl -n rig-staging-operations get pods -l app.kubernetes.io/name=argocd-repo-server \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[?(@.name=="cmp-server")].image}{"\n"}{end}'
kubectl -n rig-staging-operations get applications.argoproj.io
```

## Productie

Dezelfde stappen met `CLUSTER_TYPE=odcn-production`, nadat staging gezond is en er een besluit ligt. Twee verschillen:

- `production-infrastructure` synct automatisch vanaf `main`. Een merge naar `main` rolt dus alles uit wat onder `infrastructure/` en de door ArgoCD beheerde delen van `bootstrap/` verandert. Kijk vóór de merge welke paden dat zijn.
- Wijzigingen op productie gaan alleen via git en deze taken. Read-only kubectl mag; met de hand patchen niet.

## Terugrollen

Pin een eerdere tag uit de git-historie van het pinbestand en deploy opnieuw; er wordt niets herbouwd. Voor de OPI: `task pin-operations-manager-image IMAGE_TAG=<tag>`, voor de sidecar: `task pin-cmp-image IMAGE_TAG=<tag>`. Zie [features/image-versioning-calver.md](../../features/image-versioning-calver.md#terugrollen).
