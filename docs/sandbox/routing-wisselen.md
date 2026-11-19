# De sandbox wisselen tussen ingress-nginx en de Gateway API

ingress-nginx is sinds maart 2026 gearchiveerd, dus ZAD gaat naar de Gateway API. De sandbox
kan daarom in twee standen staan, en de wissel is een handeling van een paar minuten op een
draaiend cluster. Dit stuk zegt welke twee standen er zijn, welke regels je omzet, welk
commando het cluster bijtrekt, en wat er tijdens de wissel plat ligt.

## De twee standen

| | `ingress` | `gateway` |
|---|---|---|
| Ingang | ingress-nginx in namespace `ingress-nginx` | Envoy Gateway in `envoy-gateway-system`, met de gedeelde Gateway `rig-gateway` in `rig-gateway` |
| Per dienst | een `Ingress` | een `HTTPRoute` aan de listener `https` |
| TLS | het wildcard-secret per Ingress, plus `--default-ssl-certificate` op de controller voor tenant-namespaces | eenmaal op de listener `https`, met een `ReferenceGrant` in `rig-system` |
| Poort 80 | nginx stuurt zelf naar https | een `HTTPRoute` zonder hostname met een `RequestRedirect` |
| Timeouts | `proxy-read-timeout`/`proxy-send-timeout` per Ingress | `timeouts.request: 0s` per route, plus een `BackendTrafficPolicy` op de Gateway voor de idle-timeout |
| ArgoCD | `argocd-server` serveert TLS, de Ingress praat HTTPS met de backend | `server.insecure`, dus plain HTTP achter de Gateway; OPI praat in die stand op poort 80 (`ARGOCD_PORT`/`ARGOCD_USE_TLS` in het gateway-component) |

`HTTPRoute` staat in beide standen op de `namespaceResourceWhitelist` van het AppProject
`zad-platform`: een soort die daar ontbreekt laat ArgoCD de HELE sync van
`sandbox-infrastructure` en `ron-infrastructure` weigeren.

Beide standen binden poort 80 en 443 van de kind-node met een `hostPort`, want kind mapt
alleen die twee poorten naar de host (`bootstrap/sandbox/kind-config.yaml`). Daar kan er maar
een van de twee op zitten.

## De drie regels die de stand kiezen

Alle drie tegelijk omzetten; de toets
`operations-manager/python/tests/test_sandbox_routing_stand.py` houdt ze gelijk.

1. `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`:
   `components: - ../../infrastructure/routing/<stand>` (Keycloak, Forgejo, MinIO,
   Prometheus, de registry).
2. `bootstrap/rig-system/kustomize/overlays/sandboxed-local/kustomization.yaml`:
   `components: - ../../components/sandbox-routing/<stand>` (het portaal en ArgoCD).
3. `CLUSTER_CONFIG["sandboxed-local"]["ingress"]["routing"]` in
   `operations-manager/python/opi/core/cluster_config.py`: wat OPI per deployment rendert.

De mailsink staat onder een eigen ArgoCD Application (`ron-infrastructure`, namespace
`rig-ron`), die de clusterbuild van `rig-system` niet kan meeliften. Zijn stand staat daarom
in een vierde regel, in
`infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local/kustomization.yaml`:
`components: - ../../../../routing/mailsink-<stand>`.

## De wissel

```bash
task sandbox:routing-apply
```

De taak leest de stand uit regel 2 en brengt het cluster daarnaartoe:

1. de controller van de nieuwe stand installeren en afwachten (in gateway-stand tot de
   Gateway `Programmed=True` is). Faalt dit, dan is er nog niets weggehaald;
2. de controller van de andere stand verwijderen, inclusief zijn cluster-scoped delen;
3. de bootstrap-laag toepassen en de routing-resources van de andere stand daar weghalen;
4. `task sandbox:sync` plus een hard refresh, zodat ArgoCD de platformroutes van de andere
   stand prunet;
5. de CoreDNS-rewrite naar het nieuwe doel zetten;
6. OPI de projecten laten herverwerken, zodat de tenant-manifesten de nieuwe vorm krijgen.
   Dat endpoint vraagt het persoonlijke token van een platformbeheerder plus een verse
   step-up (`features/platformbeheer-via-de-api.md`), dus zet `ZAD_TOKEN` in de omgeving;
   zonder dat token meldt de taak welk commando je met de hand moet draaien;
7. controleren dat er niets van de andere stand over is. Is de herverwerking uit stap 6
   gestart, dan wacht deze controle tot tien minuten op de tenant-resources; daarna is een
   rest een fout, met de namen erbij.

Faalt de controle, dan hangt de herverwerking of is ze nooit gestart: volg de log van OPI
(`kubectl -n rig-system logs deploy/operations-manager -f`) en draai de taak opnieuw.

## Wat er tijdens de wissel plat ligt

Poort 80 en 443 van het cluster, zolang de oude controller nog op de hostPort zit en de
nieuwe er nog niet op kan. Dat is een korte onderbreking van alles op
`*.sandbox.rijksapp.dev`, en die is geaccepteerd: een tweede ingang naast de eerste kan niet.

## Terug wisselen

Dezelfde weg, de andere kant op: zet de drie regels (plus die van de mailsink) terug op de
andere waarde en draai `task sandbox:routing-apply` opnieuw. De taak ruimt dan de controller
en de routes van de stand op die je verlaat. In ingress-stand verdwijnen ook de CRD's van de
Gateway API, en daarmee elke `HTTPRoute`, `TLSRoute` en `ListenerSet` in het cluster.

## Een verse sandbox

`task sandbox:setup` installeert de ingang van de stand in git, via
`task install-routing-controller`. Er is dus niets extra's te doen: zet de regels om voordat
je een sandbox opbouwt, en hij komt in die stand op.

## Versies

| | Versie |
|---|---|
| Envoy Gateway | v1.9.4, gepind in `bootstrap/rig-system/kustomize/envoy-gateway/base/kustomization.yaml` |
| Gateway API CRD's | v1.6.1, experimental channel, uit dat zelfde installatiemanifest |
| ingress-nginx | controller-v1.15.1, gepind in `bootstrap/rig-system/kustomize/ingress-nginx/base/kustomization.yaml` |

De Gateway API CRD's komen uit het installatiemanifest van Envoy Gateway zelf, dus ze worden
niet apart geinstalleerd. Het experimental channel is hier nodig noch hinderlijk: het is een
superset van standard, en `ListenerSet` en `TLSRoute` zitten in beide.
