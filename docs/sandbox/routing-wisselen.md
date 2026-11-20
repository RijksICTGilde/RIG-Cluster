# De sandbox wisselen tussen ingress-nginx en de Gateway API

ingress-nginx is sinds maart 2026 gearchiveerd, dus ZAD gaat naar de Gateway API. De sandbox
kan daarom in twee standen staan. De wissel is sinds RC-513 een regel in git, en de rest doen
ArgoCD en OPI zelf.

## De wissel

Zet in `infrastructure/bootstrap/routing/sandboxed-local/kustomization.yaml` de ene
`components`-regel om:

```yaml
components:
  - ./gateway     # of ./ingress
```

Push die wijziging en draai:

```bash
task sandbox:sync
```

Dat is alles. Er is geen taak meer die de stand van het cluster verandert.

## Wat er daarna gebeurt, en in welke volgorde

1. **ArgoCD** ziet de wijziging in de Application `routing-infrastructure`. Die beheert de
   namespaced helft van de ingang: de route van elke platformdienst, de gedeelde Gateway met
   zijn `EnvoyProxy` en `BackendTrafficPolicy` of de controller van ingress-nginx, de
   ExternalName-Service `zad-ingang`, en de ConfigMap `zad-routing`.
2. ArgoCD **prunet eerst en applyt daarna**. Dat is de default en het is hier wezenlijk:
   beide standen binden hostPort 80 en 443 van de kind-node, dus de pod van de nieuwe stand
   blijft `Pending` zolang de oude er nog op zit. Daarom staat er geen `PruneLast` op deze
   Application.
3. **OPI** leest de ConfigMap `zad-routing` elke minuut (`RoutingModeWatcher`). Zolang de
   ingang van de nieuwe stand nog niet klaar is (de Gateway niet `Programmed`, of geen
   IngressClass) blijft OPI in de oude stand renderen: een project dat iemand halverwege
   bewerkt komt dus niet op een ingang die er nog niet is.
4. Zodra die ingang klaar is, gaat OPI in de nieuwe stand renderen en zet hij voor **alle
   projecten** een herverwerking in de rij, via dezelfde weg als
   `POST /api/v2/admin/projects/:reprocess-all`. Dat gebeurt een keer per wissel, ook over
   herstarts heen: de overgenomen stand staat in de tabel `routing_mode`.
5. ArgoCD prunet daarna de tenant-resources van de oude stand, zodra de herverwerking de
   nieuwe vorm naar `zad-deployments` heeft geschreven.

De controllers zelf gaan hier niet mee om. Die zijn **platform**: ze staan er altijd, in
beide standen.

## De platformlaag: beide controllers staan er altijd

`task sandbox:routing-bootstrap` installeert de controller van Envoy Gateway met de CRD's van
de Gateway API, de GatewayClass, en van ingress-nginx de cluster-scoped helft (de namespace,
de cluster-RBAC, de IngressClass `nginx` en de admission-webhook). Ongeacht de stand in git,
en ArgoCD raakt die resources niet aan: het AppProject `zad-routing` heeft
`clusterResourceWhitelist: []`. Daarmee vraagt een wissel nooit een bootstrapstap.

Dat is ook hoe het op een ander cluster werkt, maar dan andersom: **op ODCN en Fundament
levert het platform de controllers en de CRD's zelf**, en ZAD levert alleen de routes. De
sandbox regelt die laag zelf omdat er geen platform onder zit. De paden:
`infrastructure/bootstrap/routing/platform/sandboxed-local/`.

Wat per stand WEL wisselt, is wat hostPort 80 en 443 vasthoudt: de controller-Deployment van
ingress-nginx, of de proxy die Envoy Gateway uit de Gateway maakt. Die controller-Deployment
staat daarom bij de stand en niet bij het platform.

### De admission-webhook van nginx blijft in gateway-stand staan

De `ValidatingWebhookConfiguration` `ingress-nginx-admission` is platform, dus hij staat er
ook in gateway-stand, terwijl zijn backend (`ingress-nginx-controller-admission`) dan weg is.
Hij heeft `failurePolicy: Fail`, dus dat is niet gratis, maar het is acceptabel: hij
valideert alleen CREATE en UPDATE van een `Ingress`, en in gateway-stand maakt OPI geen
Ingresses. Het prunen van de Ingresses van de oude stand is een DELETE en gaat er langs.
`test_the_admission_webhook_only_validates_creates_and_updates` houdt dat vast; verandert
het manifest van een nieuwe versie dat, dan valt die toets om.

Wil je in gateway-stand toch met de hand een Ingress maken, dan weigert de apiserver dat met
`failed calling webhook`. Dat is geen storing maar deze webhook.

## Hoe je ziet dat de wissel klaar is

```bash
# De ingang zelf
kubectl -n rig-system get application routing-infrastructure
#   Synced / Healthy

# De stand die OPI leest en de stand waarin hij rendert
kubectl -n rig-system get configmap zad-routing -o jsonpath='{.data.routing}'
kubectl -n rig-system logs deploy/operations-manager | grep -i "routing-stand"
#   "OPI rendert vanaf nu in routing-stand 'gateway' (...)"
#   "Routing-wissel naar 'gateway': N project(en) in de rij voor herverwerking"

# En dat er niets van de andere stand over is
kubectl get ingress -A              # leeg in gateway-stand
kubectl get httproute -A            # leeg in ingress-stand
kubectl -n ingress-nginx get deploy # leeg in gateway-stand
kubectl -n rig-gateway get gateway  # leeg in ingress-stand
```

De CRD's en de controllers blijven in beide standen staan: die zijn platform. Wat weg hoort te
zijn, is wat de stand zelf meebrengt.

## Wat er tijdens de wissel plat ligt

Poort 80 en 443 van het cluster, zolang de oude controller van de hostPort af is en de nieuwe
er nog niet op zit. Dat is een korte onderbreking van alles op `*.sandbox.rijksapp.dev`, en
die is geaccepteerd: een tweede ingang naast de eerste kan niet. ArgoCD is in die tijd zelf
ook onbereikbaar via `argo.sandbox.rijksapp.dev`; `kubectl -n rig-system port-forward
svc/argocd-server 8080:80` werkt wel.

## De twee standen

| | `ingress` | `gateway` |
|---|---|---|
| Ingang | ingress-nginx in namespace `ingress-nginx` | Envoy Gateway in `envoy-gateway-system`, met de gedeelde Gateway `rig-gateway` in `rig-gateway` |
| Per dienst | een `Ingress` | een `HTTPRoute` aan de listener `https` |
| TLS | het wildcard-secret per Ingress, plus `--default-ssl-certificate` op de controller voor tenant-namespaces | eenmaal op de listener `https`, met een `ReferenceGrant` in `rig-system` |
| Poort 80 | nginx stuurt zelf naar https | een `HTTPRoute` zonder hostname met een `RequestRedirect` |
| Timeouts | `proxy-read-timeout`/`proxy-send-timeout` per Ingress | `timeouts.request: 0s` per route, plus een `BackendTrafficPolicy` op de Gateway voor de idle-timeout |
| HSTS | nginx stuurt de header zelf | een `ResponseHeaderModifier` per route, met dezelfde waarde |
| CoreDNS | `zad-ingang` wijst naar `ingress-nginx-controller.ingress-nginx` | `zad-ingang` wijst naar `rig-gateway.envoy-gateway-system` |

ArgoCD staat op de sandbox in **beide** standen op `server.insecure`, dus TLS eindigt altijd
op de ingang en `argocd-server` praat plain HTTP. Daarmee hangen de `ARGOCD_*`-instellingen
van OPI niet meer aan de stand: ze staan vast op poort 80 zonder TLS in
`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml`.

## De vaste ingang voor CoreDNS

`task sandbox:configure-coredns` herschrijft `*.sandbox.rijksapp.dev` naar
`zad-ingang.rig-system.svc.cluster.local` en verandert daarna nooit meer. `zad-ingang` is een
ExternalName-Service die elke stand zelf meebrengt, naar de Service van zijn eigen controller.

## Een verse sandbox

`task sandbox:setup` draait `task sandbox:routing-bootstrap`: eerst de platformlaag hierboven,
en daarna eenmalig de stand uit git, voordat ArgoCD bestaat. Zonder ingang krijgt geen Ingress
of HTTPRoute een adres en komt het clonen over `https://forgejo.sandbox.rijksapp.dev` niet op
gang. Die tweede stap is geen wisselstap: de Application `routing-infrastructure` neemt die
resources daarna over en is vanaf dan de enige die ze beheert.

## De aansluiting tussen de twee helften

OPI rendert de routes van een project, en de routing van het cluster rendert de Gateway, zijn
listeners en de grants waar die routes aan hangen. Dat de twee op elkaar passen staat in
`operations-manager/python/tests/test_sandbox_routing_contract.py`: elke `parentRef`, de
`allowedListeners`, de `allowedRoutes`, elke `certificateRefs`, de `controller_selector` uit
`CLUSTER_CONFIG`, en de validatie van elk Gateway API-object tegen de CRD's van de gepinde
versie.

## Versies

| | Versie |
|---|---|
| Envoy Gateway | v1.9.4, gepind in `infrastructure/bootstrap/routing/platform/sandboxed-local/envoy-gateway/kustomization.yaml` |
| Gateway API CRD's | v1.6.1, experimental channel, uit dat zelfde installatiemanifest |
| ingress-nginx | controller-v1.15.1, gepind in `infrastructure/bootstrap/routing/platform/sandboxed-local/ingress-nginx/kustomization.yaml` en in `infrastructure/bootstrap/routing/sandboxed-local/ingress/controller/kustomization.yaml`; de twee halves dragen dezelfde pin en een toets meet dat |

De Gateway API CRD's komen uit het installatiemanifest van Envoy Gateway zelf, dus ze worden
niet apart geinstalleerd. Het experimental channel is hier nodig noch hinderlijk: het is een
superset van standard, en `ListenerSet` en `TLSRoute` zitten in beide. De testfixture
`operations-manager/python/tests/fixtures/gateway-api-crds-v1.6.1.yaml` draagt dezelfde pin
en gaat mee bij een bump.
