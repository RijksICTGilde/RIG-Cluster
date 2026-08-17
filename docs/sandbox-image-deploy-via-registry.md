# Sandbox: deploy een image via de in-cluster registry i.p.v. `kind load`

Een build op de dev-sandbox neerzetten met `kind load docker-image` is traag: `kind load`
exporteert **elke keer de hele image als tar** uit de docker-daemon en importeert die in de
containerd van de kind-node. Er is geen laag-hergebruik, dus zelfs een derived image dat
alleen een dunne `COPY`-laag toevoegt streamt de volledige (honderden MB) base opnieuw. Dat
is dubbel werk: build in docker, dan nog eens volledig overzetten naar containerd.

De sandbox heeft al een **in-cluster registry** (`rig-registry`). Via die registry deployen
vervangt de tar-transfer door een gewone `docker push`, die alleen de gewijzigde lagen
verstuurt (de registry dedupt op digest). Iteratief deployen wordt daarmee seconden i.p.v.
minuten.

## Wat er al staat

- **`rig-registry`** draait in `rig-system` (ClusterIP `:5000`), ontsloten via ingress op
  `registry.sandbox.rijksapp.dev` met het geldige wildcard-cert. De kind-node resolvet die
  hostnaam naar `127.0.0.1` (ingress-nginx op hostPort 443), dus er zijn geen
  containerd-aanpassingen nodig.
- Credentials: `admin` / `admin1234` (uit de operations-manager configmap).
- Een pull-secret bestaat al: **`rig-registry-pull`** (`kubernetes.io/dockerconfigjson`) in
  `rig-system`, oorspronkelijk aangemaakt voor de ArgoCD-serviceaccounts.

## De flow

```bash
IMG=registry.sandbox.rijksapp.dev/operations-manager:rc-$(git rev-parse --short HEAD)

# 1. Bouwen (derived image bovenop de huidige base; kopieer OOK manifests/, niet alleen opi/)
docker build --network=host -t "$IMG" -f Dockerfile operations-manager/python

# 2. Pushen naar de in-cluster registry (eenmalig eerst: docker login)
docker login registry.sandbox.rijksapp.dev -u admin -p admin1234
docker push "$IMG"          # alleen gewijzigde lagen; base gaat maar één keer

# 3. Eenmalig: de kubelet laten pullen met de bestaande pull-secret
kubectl -n rig-system patch deploy operations-manager --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/imagePullSecrets","value":[{"name":"rig-registry-pull"}]}]'

# 4. Image zetten en uitrollen
kubectl -n rig-system set image deploy/operations-manager operations-manager="$IMG"
kubectl -n rig-system rollout status deploy/operations-manager
```

Met een unieke tag per commit volstaat `imagePullPolicy: IfNotPresent` (de kubelet pullt de
tag één keer). De eerste push bevat de base-lagen (eenmalig groot); daarna reist alleen de
kleine `COPY opi/manifests`-laag.

## Waarom dit beter is dan `kind load`

| | `kind load docker-image` | registry push + pull |
|---|---|---|
| Overdracht | volledige image-tar, elke keer | alleen gewijzigde lagen |
| Laag-hergebruik | geen | ja (digest-dedup in de registry) |
| Iteratief deployen | minuten | seconden |
| Extra plumbing | geen | eenmalig pull-secret op de deployment |

## Waar dit nog breder doorgevoerd moet worden

Dit patroon hoort op meer plekken thuis dan een handmatige sessie-deploy; vanaf dit document
kunnen die worden bijgewerkt:

- **`sandbox-deploy`** (de baked dclaude-command) en `task sandbox:update-operations-manager`:
  laat die pushen naar `rig-registry` en de deployment naar de registry-tag zetten in plaats
  van `kind load`.
- **De operations-manager overlay** (`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local`):
  de `imagePullSecrets: [rig-registry-pull]` daar vastleggen, zodat de kubelet standaard uit
  `rig-registry` kan pullen en de patch bij stap 3 niet meer per sessie nodig is.
- **`workflow/sandbox.md`**: de deploy-sectie verwijzen naar deze flow.

Zie ook `docs/sandbox-on-dev-server.md` (Caddy/poorten) en `images/argocd-rig/README.md`,
waar dezelfde registry al voor de argocd-rig image wordt gebruikt.
