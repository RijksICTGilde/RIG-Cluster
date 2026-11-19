# Routing per clustertype: Ingress of Gateway API

ingress-nginx is sinds maart 2026 gearchiveerd. Een instelling per clustertype bepaalt
daarom of OPI voor projecten `Ingress`-resources rendert (zoals altijd) of Gateway
API-resources. Bij een wissel ruimt OPI de resources van de andere stand op, zodat een
cluster zonder handwerk van de ene naar de andere stand kan.

Alle clustertypes staan nu op `ingress`. De gateway-stand is met tests bewezen, niet met
een uitrol: er verandert niets aan de output van sandbox, staging of productie.

## De instelling

```python
CLUSTER_CONFIG["sandboxed-local"]["ingress"] = {
    "enable_tls": True,
    "ip_whitelist": "0.0.0.0/0,::/0",
    "routing": RoutingApi.INGRESS,  # of RoutingApi.GATEWAY
    "gateway": {
        "name": "rig-gateway",
        "namespace": "rig-gateway",
        "http_listener": {"name": "http", "port": 80},
        "https_listener": {"name": "https", "port": 443, "hostname": "*.sandbox.rijksapp.dev"},
        "controller_selector": {
            "namespace": "envoy-gateway-system",
            "pod_labels": {
                "gateway.envoyproxy.io/owning-gateway-name": "rig-gateway",
                "gateway.envoyproxy.io/owning-gateway-namespace": "rig-gateway",
            },
        },
    },
}
```

Ontbreekt `routing`, dan geldt `ingress`. De getters staan in `opi/core/cluster_config.py`:
`get_routing_api`, `renders_gateway_api` en `get_gateway_config` (die weigert in
ingress-stand). `get_ingress_controller_selector` geeft in gateway-stand de
`controller_selector` uit het gateway-blok, zodat elke NetworkPolicy die de ingang toelaat
vanzelf meeschuift.

**Guard op `ip_whitelist`.** `get_routing_api` weigert een clusterconfig die de gateway-stand
combineert met een `ip_whitelist` die iets anders is dan open (`0.0.0.0/0,::/0` of
`0.0.0.0`). De whitelist is een HAProxy-annotatie waar de Gateway API geen equivalent voor
heeft, dus stil wegvallen zou een afscherming opheffen.

## Wat OPI rendert

De gedeelde Gateway is van de infrastructuur, niet van OPI. OPI raakt hem nooit aan en hangt
er alleen routes en een ListenerSet aan. Per project en deployment, in de tenant-namespace:

| Stand `ingress` | Stand `gateway` |
|---|---|
| `Ingress` per hostname en pad | `HTTPRoute` per hostname en pad |
| `ssl-passthrough`-Ingress | `TLSRoute` plus een passthrough-listener |
| cert-manager-annotaties op de Ingress | een `ListenerSet` met die annotaties |

De keuze per hostname valt in `publication_for` (`opi/manager/project/manifests/routing.py`):

- een hostname die het certificaat van de Gateway zelf dekt (de wildcard van
  `https_listener`) hangt via `sectionName` aan die gedeelde listener;
- elke andere hostname claimt een listener in de `ListenerSet` van de deployment. Een
  wildcard dekt precies een label, dus een gestippeld adres (`poc.app.sandbox.rijksapp.dev`)
  krijgt ook een eigen listener;
- een passthrough-hostname krijgt een listener met `protocol: TLS` en `tls.mode: Passthrough`,
  en een `TLSRoute`.

OPI hangt niets aan de `http_listener`. De infrastructuur zet daar een HTTPRoute zonder
hostname die alles naar https doorstuurt; de challenge-routes van cert-manager zijn
specifieker en gaan voor.

### De ListenerSet

Een listener is **per hostname** en niet per component: meerdere componenten kunnen op een
hostname publiceren en moeten allemaal aan dezelfde listener hangen, anders ziet de Gateway
twee listeners op een hostname en weigert hij ze als conflict. De naam volgt daarom uit de
hostname (`generate_listener_name`), en van de certificaat-Secrets van die componenten wint
de eerste in sorteerorde. Dat is dezelfde Secret die de Ingress van die hostname nu al
gebruikt, dus een wissel vraagt geen nieuw certificaat aan.

Er zijn **twee** ListenerSets per deployment, en alleen die met listeners worden geschreven:

- `<deployment>-listeners` draagt de cert-manager-annotaties (`cert-manager.io/issuer` of
  `cert-manager.io/cluster-issuer`, het sleutelalgoritme, de rotatie en
  `acme.cert-manager.io/http01-parentreffallback`);
- `<deployment>-own-listeners` draagt geen annotatie: hier staan de hostnames met een door de
  klant aangeleverd certificaat (`tls: provided`) en de passthrough-hostnames.

De splitsing is nodig omdat `cert-manager.io/issuer` voor de HELE ListenerSet geldt: een
aangeleverd certificaat in de geannoteerde set zou door cert-manager worden overschreven. Het
certificaat van een listener staat in een Secret in de eigen namespace, dus er is geen
ReferenceGrant nodig.

De Gateway laat ListenerSets toe uit namespaces met het label
`created-by: operations-manager`, dat `manifests/namespace.yaml.jinja` al zet.

### De Issuer

`manifests/issuer-letsencrypt.yaml.jinja` krijgt in gateway-stand een HTTP-01-solver
`gatewayHTTPRoute` met een `parentRef` naar de `http_listener` van de Gateway, in plaats van
`http01.ingress`.

## Hoe de annotaties overgaan

| Nu op de Ingress | Gateway-stand |
|---|---|
| rewrite in `configuration-snippet` | `URLRewrite`-filter met `ReplacePrefixMatch` |
| `more_set_headers` (vier securityheaders) en HSTS | `ResponseHeaderModifier`-filter met dezelfde headers en waarden |
| `proxy-read-timeout` / `proxy-send-timeout` (300s) | `rules[].timeouts.request: "0s"` (uit) |
| `proxy-body-size`, `proxy-buffer-size`, `proxy-buffers-number`, `proxy-connect-timeout` | vervalt |
| `ip_whitelist` (HAProxy) | vervalt, en de guard hierboven weigert een dichte whitelist |
| `external-dns.alpha.kubernetes.io/target` | dezelfde annotatie op de HTTPRoute en TLSRoute |
| cert-manager-annotaties | op de ListenerSet |

Over de timeout: `timeouts.request` begrenst het HELE verzoek, niet de tijd tussen twee
reads. De 300s overnemen zou downloads, uploads, SSE en websockets afbreken, en hem weglaten
ook, want Envoy zet dan een harde 15 seconden. Daarom staat hij uit; de idle-grens komt van
de controller (Envoy: een stream idle timeout van 5 minuten, gelijk aan de huidige 300s).

De body-grens en de buffers vervallen: Envoy begrenst de body niet zolang er niet gebufferd
wordt, en buffers zijn nginx-specifiek. Er komt geen vendor-policy voor in de plaats.

## Een cluster omzetten

1. **Infrastructuur eerst.** Gateway API CRD's v1.5 of hoger (standard channel), een
   controller die `ListenerSet` v1 en `TLSRoute` v1 serveert (Envoy Gateway 1.9 doet dat), de
   gedeelde Gateway met beide listeners en het wildcardcertificaat van de clusterzone, en de
   HTTPRoute op de http-listener die alles naar https stuurt. Voor eigen domeinen:
   cert-manager 1.20 of hoger met `config.gatewayAPI.enabled`,
   `config.gatewayAPI.enableListenerSet` en de feature gate `ListenerSets`.
2. **Zet `routing` op `gateway`** voor dat clustertype en rol OPI uit.
3. **`POST /api/v2/admin/projects/:reprocess-all`** (platform-admin-token plus step-up). Dat
   zet per project een gewone reprocess-taak in de bestaande takenrij, een tegelijk. Het
   antwoord noemt het aantal ingeplande taken.
4. **Controleer dat er niets van de oude stand over is**: geen `Ingress` meer in de
   tenant-namespaces, en geen Ingress-bestand meer in `zad-deployments`.

De grendel op stap 3: in gateway-stand moet de geconfigureerde Gateway bestaan en conditie
`Programmed=True` hebben, in ingress-stand moet er minstens een IngressClass zijn. Niet per
se een default: de gegenereerde Ingresses zetten geen `ingressClassName` en de controller
pakt een klasseloze Ingress op. Op de sandbox gemeten: de enige IngressClass daar draagt geen
default-annotatie, en publiceren werkt er. Een
definitief "bestaat niet" geeft 409 met uitleg; een fout bij het opvragen (timeout, apiserver
weg) geeft 503 en leest nooit als "bestaat niet". Zo zet niemand elk project om naar een
ingang die er niet is.

## Hoe de opruiming werkt

**De projectmanifests gaan via GitOps.** Na een wissel schrijft een reprocess alleen nog
bestanden van de nieuwe stand. `select_obsolete_component_manifests`
(`opi/manager/project/manifests/pruning.py`) haalt de rest uit git, en ArgoCD verwijdert de
objecten (`prune: true`, `PruneLast`). De bestandsnamen in gateway-stand:
`<component>-httproute[-<suffix>].yaml` en `<component>-tlsroute[-<suffix>].yaml`, plus de
root- en kaal-domein-varianten `-httproute-root` en `-httproute-bare-domain`.

De ListenerSet is de uitzondering: hij heet `listenerset.yaml` en is deployment-breed, dus hij
heeft geen component-prefix en de prune ziet hem niet. `remove_listener_set` haalt hem weg in
elke ronde die geen enkele listener claimt, de ingress-stand incluis.

**De databaseconsole gaat niet via GitOps** maar wordt direct met kubectl toegepast
(`opi/manager/db_console_manager.py`). Die rendert in gateway-stand een HTTPRoute op de
gedeelde listener, want de console-hostname zit in de clusterzone. De label-gestuurde
opruiming van een run-bundel (`bundle_kinds` in `opi/manager/run_support.py`) neemt HTTPRoute
mee zodra het cluster dat type serveert, ongeacht de ingestelde stand, zodat een console die
voor de wissel is gestart ook verdwijnt. Niet onvoorwaardelijk: een type dat het cluster niet
heeft laat kubectl het hele commando weigeren.

## Rechten

**OPI zelf** leest voor de grendel de IngressClasses van het cluster, dus
`namespace-manager` (de ClusterRole in `bootstrap/rig-system/kustomize/operations-manager/`)
heeft `get, list` op `ingressclasses`. Voor de gateway-stand heeft OPI daarnaast leesrecht op
`gateways` in `gateway.networking.k8s.io` nodig; dat komt met de CRD's uit de infra-taak.

Projectleden krijgen hier niets bij. OPI maakt voor projectleden geen Role of RoleBinding
(`manifests/` heeft er geen; de enige RoleBinding is die van de CMP-sleutellezer), dus zij
kunnen in hun namespace met kubectl noch een Ingress noch een
`gateway.networking.k8s.io`-resource maken. Alleen OPI schrijft manifests in
tenant-namespaces, via ArgoCD, en de hostnames komen uit de bestaande validatie van
publish-on-web, inclusief de uniciteitscontrole en de gate op het kale domein.

## Waar het zit

| Onderdeel | Plek |
|---|---|
| De instelling en de getters | `opi/core/cluster_config.py` |
| De keuze per hostname en de ListenerSet | `opi/manager/project/manifests/routing.py` |
| De emitters | `opi/manager/project/manifests/application.py` |
| De sjablonen | `manifests/httproute.yaml.jinja`, `tlsroute.yaml.jinja`, `listenerset.yaml.jinja` |
| De grendel op de bulk-herverwerking | `opi/services/routing_readiness.py` |
| Het endpoint | `opi/api/admin_router.py` |
| Toetsen | `tests/test_gateway_routing.py`, `tests/test_admin_reprocess_all.py`, de goldens in `tests/golden/pipeline-gateway/` en `tests/golden/manifests/` |

Verwant: [Publish-on-web: TLS modes](publish-on-web-tls-modes.md),
[External Domains with Let's Encrypt](external-domains-letsencrypt.md),
[Multi-Path Ingress](multi-path-ingress.md).
