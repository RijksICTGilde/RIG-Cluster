# Wijzigingsverzoek: HTTP/2 aanzetten op de ingress-controller

**Status**: Doorgevoerd en geverifieerd op 2026-09-04
**Datum**: 2026-08-03, bijgewerkt 2026-09-04
**Prioriteit**: Middel, raakt laadtijd van asset-zware applicaties, geen functionele blokkade
**Aanvrager**: RIG / ZAD, namens project regelrecht (`rig-prd-regel-k4c`)

## Uitkomst

Het platformteam heeft de annotatie op 4 september 2026 gezet. De ingress-controllers zijn opnieuw gestart en HTTP/2 is daarna bevestigd in productie.

De gemeten werkelijkheid komt overeen met wat in dit document werd voorspeld: routes met een eigen en uniek certificaat spreken h2, de rest blijft HTTP/1.1. De metingen staan onderaan onder [Verificatie na doorvoeren](#verificatie-na-doorvoeren). De rest van het document blijft ongewijzigd staan als onderbouwing en als vastlegging van de afweging.

## Probleem

Op alle ZAD-hostnames in productie komt ALPN niet tot een afspraak. De client biedt `h2,http/1.1` aan, de router antwoordt niet en valt terug op HTTP/1.1.

```
$ curl -v https://editor.regelrecht.rijks.app
* ALPN: curl offers h2,http/1.1
* SSL connection using TLSv1.3 / AEAD-CHACHA20-POLY1305-SHA256
* ALPN: server did not agree on a protocol. Uses default.
*  subject: CN=editor.regelrecht.rijks.app
*  issuer: C=US; O=Let's Encrypt; CN=YE1
* using HTTP/1.x
> GET / HTTP/1.1
< HTTP/1.1 200 OK
```

Ook met geforceerde `--http2` komt HTTP/1.1 terug. Hetzelfde beeld op elke andere geteste hostname:

| Hostname | Protocol | Status |
|---|---|---|
| editor.regelrecht.rijks.app | 1.1 | 200 |
| docs.regelrecht.rijks.app | 1.1 | 200 |
| zad.rijksapp.nl | 1.1 | 302 |
| keycloak.rijksapp.nl | 1.1 | 200 |
| bouwmeester.rijks.app | 1.1 | 200 |
| amt.rijksapp.nl | 1.1 | 200 |
| grist.rijksapp.nl | 1.1 | 200 |
| editor-pr1077-regel-k4c.rig.prd1.gn2.quattro.rijksapps.nl | 1.1 | 200 |
| console-openshift-console.apps.prd1.gn2.quattro.rijksapps.nl | 1.1 | 200 |

## Aanleiding

De editor van regelrecht laadt 23 losse JS- en CSS-assets plus een WASM-bundel. Over HTTP/1.1 opent een browser maximaal zes verbindingen per host, dus die requests staan in de rij.

Belangrijke kanttekening die wij zelf hebben vastgesteld en die eerlijk in deze afweging hoort: HTTP/2 is voor dit specifieke geval niet de grootste winst. Zie "Verhouding tot de andere maatregelen" onderaan. Wij vragen HTTP/2 omdat het de juiste default is voor de hele controller, niet als oplossing voor één project.

## Vaststelling: dit is geen route-eigenschap

Alle 187 routes die wij vanuit onze tenant-rechten kunnen zien, worden bediend door één IngressController:

```
routerName=rig
routerCanonicalHostname=router-rig.rig.prd1.gn2.quattro.rijksapps.nl
```

Daarvan dragen er 57 een eigen certificaat mee en voldoen dus aan alle HAProxy-voorwaarden voor ALPN. Geen enkele daarvan onderhandelt h2. Het staat dus uit op controller- of clusterniveau, niet per route. Er bestaat in OpenShift ook geen route-annotatie om HTTP/2 per route aan te zetten, dus een oplossing binnen ons eigen bereik is er niet.

Wij kunnen niet vaststellen op welk van de twee niveaus het uit staat, omdat we geen cluster-scoped leesrechten hebben:

```
Error from server (Forbidden): ingresscontrollers.operator.openshift.io is forbidden:
User "robbert.uittenbroek" cannot list resource "ingresscontrollers"
in API group "operator.openshift.io" in the namespace "openshift-ingress-operator"

Error from server (Forbidden): ingresses.config.openshift.io "cluster" is forbidden:
User "robbert.uittenbroek" cannot get resource "ingresses"
in API group "config.openshift.io" at the cluster scope
```

## Waarschijnlijke oorzaak: dit is de platformdefault

Het cluster draait Kubernetes v1.33, dus OpenShift 4.20. In die versie is HTTP/2 nog steeds standaard uit. Uit `cluster-ingress-operator`, branch `release-4.20`, `pkg/operator/controller/ingress/deployment.go`:

```go
if HTTP2IsEnabled(ci, ingressConfig) {
    env = append(env, corev1.EnvVar{Name: RouterDisableHTTP2EnvName, Value: "false"})
} else {
    env = append(env, corev1.EnvVar{Name: RouterDisableHTTP2EnvName, Value: "true"})
}
```

De volgorde is: de annotatie op de IngressController wint, en pas als die ontbreekt kijkt de operator naar de clusterbrede ingress-config.

```go
func HTTP2IsEnabled(ic *operatorv1.IngressController, ingressConfig *configv1.Ingress) bool {
    controllerHasHTTP2Annotation, controllerHasHTTP2Enabled := HTTP2IsEnabledByAnnotation(ic.Annotations)
    _, configHasHTTP2Enabled := HTTP2IsEnabledByAnnotation(ingressConfig.Annotations)

    if controllerHasHTTP2Annotation {
        return controllerHasHTTP2Enabled
    }

    return configHasHTTP2Enabled
}
```

Voor de `rig`-controller is dus geen van beide gezet, anders hadden we h2 gemeten.

## Wat wij vragen

De annotatie op de IngressController die onze routes bedient:

```bash
oc -n openshift-ingress-operator annotate ingresscontrollers/rig \
  ingress.operator.openshift.io/default-enable-http2=true
```

Onze voorkeur gaat uit naar deze controller-variant boven de clusterbrede (`oc annotate ingresses.config/cluster ...`), omdat die het effect beperkt tot de router waar onze routes op zitten.

## Reikwijdte: het geldt niet voor alle RIG-routes

Het platformteam merkte terecht op dat de annotatie op controllerniveau staat en daarmee alle RIG-routes raakt. Dat is voor de annotatie waar, maar de router beperkt het effect vervolgens zelf. Uit `openshift/router`, branch `release-4.20`, `pkg/router/template/template_helper.go:222`:

```go
if td.DisableHTTP2 || td.CertificateIndex[cert.Contents] > 1 {
    lines = append(lines, strings.Join([]string{fqCertPath, entry.Value}, " "))
} else {
    lines = append(lines, strings.Join([]string{fqCertPath, "[alpn h2,http/1.1]", entry.Value}, " "))
}
```

Een route krijgt alleen `[alpn h2,http/1.1]` in de crt-list als hij een eigen certificaat heeft én die exacte certificaatinhoud door precies één route wordt gebruikt. Routes op het default wildcard-certificaat hangen aan een bind die expliciet `no-alpn` zet:

```
bind :443 ... crt {{ .DefaultCertificate }} crt-list /var/lib/haproxy/conf/cert_config.map accept-proxy
  no-alpn
```

Die beperking bestaat om connection coalescing te voorkomen, waarbij een browser één HTTP/2-verbinding hergebruikt voor verschillende hostnames die onder hetzelfde certificaat vallen.

Toegepast op de huidige toestand van dit cluster:

| Categorie | Aantal | Effect na aanzetten |
|---|---|---|
| Routes totaal op controller `rig` | 187 | |
| Eigen én uniek certificaat | 49 | krijgen h2 |
| Gedeeld certificaat (8x `docs.rijksapp.nl`) | 8 | blijven HTTP/1.1 |
| Default wildcard-cert, waaronder ~130 PR-previews | 130 | blijven HTTP/1.1 |
| Routes `*.regelrecht.rijks.app` | 7 | krijgen h2 |

Ongeveer een kwart van onze routes dus, en die grens wordt door de router afgedwongen, niet door ons. Ontstaan er ooit twee routes met byte-identieke certificaten, dan laat de router bij beide de ALPN stilzwijgend weg en valt het terug op HTTP/1.1. Het faalt dus veilig, zonder dat iemand daarop hoeft te bewaken.

De 7 routes van regelrecht hebben elk een eigen per-host certificaat van cert-manager:

```
docs.regelrecht.rijks.app             term=edge  insecure=Redirect  eigen cert, uniek
editor.regelrecht.rijks.app           term=edge  insecure=Redirect  eigen cert, uniek
grafana.regelrecht.rijks.app          term=edge  insecure=Redirect  eigen cert, uniek
harvester-admin.regelrecht.rijks.app  term=edge  insecure=Redirect  eigen cert, uniek
lawmaking.regelrecht.rijks.app        term=edge  insecure=Redirect  eigen cert, uniek
regelrecht.rijks.app                  term=edge  insecure=Redirect  eigen cert, uniek
upload.regelrecht.rijks.app           term=edge  insecure=Redirect  eigen cert, uniek
```

## Kan het negatief werken? Vier punten

Antwoord op de vraag van het platformteam, in volgorde van hoe waarschijnlijk het is dat iemand er last van krijgt.

**1. De router herstart.** Dit is het enige punt dat álle routes op `rig` raakt, ook de 138 die op HTTP/1.1 blijven. De annotatie wijzigt `ROUTER_DISABLE_HTTP2` in de router-deployment, wat een rolling restart van de router-pods geeft. Met meerdere replicas geen downtime, maar het is een wijziging in het datapad en verdient een gepland moment.

**2. Meer gelijktijdige requests op de backends.** Dit is de realistische. De limiet van zes verbindingen per host in HTTP/1.1 werkt feitelijk als rate limiter. Over h2 kan één browser tot ongeveer 100 gelijktijdige streams op één verbinding openen. Een zwakke backend krijgt daardoor een hardere burst dan voorheen. Er is een per-route rem voor als dat nodig blijkt: `haproxy.router.openshift.io/pod-concurrent-connections`.

**3. WebSockets.** Over h2 gebruiken clients extended CONNECT (RFC 8441) in plaats van een HTTP/1.1 Upgrade. De router-image installeert `haproxy28` (`images/router/haproxy/Dockerfile`), dat ondersteunt dit, maar het is het enige functionele pad dat echt anders loopt. Van de 49 kandidaten zijn Grafana, Grist en OpenProject de voor de hand liggende websocket-gebruikers om te testen.

**4. Reencrypt-routes.** Bij `reencrypt` biedt de router ook `alpn h2,http/1.1` aan de pod aan, dus daar verandert wel iets aan de backendkant. Valt terug als de pod geen h2 adverteert, dus laag risico. Bij `edge` gebeurt dit niet en blijft het cleartext HTTP/1.1 naar de pod. De 7 routes van regelrecht zijn allemaal edge. Wij hebben de verdeling edge/reencrypt/passthrough over alle 187 routes niet kunnen vaststellen omdat onze clusterverbinding wegviel; dat leveren wij op verzoek na.

**Terugdraaien is triviaal.** Annotatie op `false` zetten of weghalen, router herlaadt. Dat maakt dit een goed omkeerbaar experiment.

## Wat er niet verandert

Alle betrokken routes van regelrecht zijn `edge`-terminated zonder `destinationCACertificate`. HAProxy blijft richting de pods cleartext HTTP/1.1 spreken. De regel `alpn h2,http/1.1` op de backend-serverregel staat achter `{{- if (eq $cfg.TLSTermination "reencrypt") }}` en geldt dus alleen voor reencrypt-routes. Applicatiecontainers hoeven niets aan te passen.

Wat wel verandert is de header `X-Forwarded-Proto-Version: h2`, die HAProxy toevoegt zodra de client via h2 binnenkomt. Applicaties die die header niet kennen, negeren hem.

## Verhouding tot de andere maatregelen

Wij hebben de editor van regelrecht doorgemeten om te bepalen hoeveel HTTP/2 daar werkelijk oplevert. De uitkomst is dat het reëel maar bescheiden is, en dat wij dat het platformteam niet willen onthouden.

De 23 assets uit de initiële HTML zijn samen 2626 KiB. De webserver van de applicatie stuurt geen enkele compressie, ongeacht wat de client aanbiedt:

```
Accept-Encoding: gzip                      -> content-length: 187102
Accept-Encoding: br                        -> content-length: 187102
Accept-Encoding: gzip, deflate, br, zstd   -> content-length: 187102
```

Met gewone `gzip -9` zou dat 723 KiB zijn, dus 72% minder bytes. Daarnaast stuurt de applicatie geen `cache-control`, terwijl de bestandsnamen content-gehasht zijn en dus `immutable` zouden kunnen zijn. En 20 van de 23 assets staan als `modulepreload` in de HTML, waaronder 557 KiB aan routeviews die pas bij navigatie nodig zijn.

De volgorde van winst is daarmee:

| Maatregel | Winst | Eigenaar |
|---|---|---|
| Compressie op de webserver van de applicatie | 2626 KiB → 723 KiB | regelrecht |
| `cache-control: public, max-age=31536000, immutable` op `/assets/*` | herhaald bezoek naar ~0 requests | regelrecht |
| Routeviews lui laden via `import()` | 557 KiB minder op eerste load | regelrecht |
| HTTP/2 op de ingress | 23 requests niet meer over 6 verbindingen | platformteam |

Drie van de vier liggen bij het project zelf en zijn inmiddels bij hen belegd. Wij vragen HTTP/2 omdat het de juiste default is voor de hele controller en omdat elk asset-zwaar project op dit platform er baat bij heeft, niet omdat het dit ene probleem oplost.

## Verificatie na doorvoeren

Gemeten op 4 september 2026, kort na de rolling restart van de router-pods.

### Protocol per categorie

De routelijst van controller `rig` is opnieuw opgehaald en per route ingedeeld naar certificaat. Het cluster is sinds de oorspronkelijke meting gegroeid van 187 naar 263 routes.

| Categorie | Routes | Gemeten |
|---|---|---|
| Eigen en uniek certificaat | 60 | HTTP/2 |
| Gedeeld certificaat (8x `docs.rijksapp.nl`) | 8 | HTTP/1.1 |
| Default wildcard-certificaat, waaronder de PR-previews | 195 | HTTP/1.1 |

Steekproeven bevestigen alle drie de categorieën. Vijftien van de vijftien geteste hosts met een eigen certificaat antwoorden h2, waaronder `zad.rijksapp.nl`, `keycloak.rijksapp.nl`, `bouwmeester.rijks.app`, `openproject.rijksapp.nl`, `grist.rijksapp.nl`, `wies.rijksapp.nl`, `digitaledienst.overheid.nl` en alle zeven hosts onder `*.regelrecht.rijks.app`. Zes willekeurig gekozen preview-routes op het wildcard-certificaat blijven HTTP/1.1, en de acht routes op het gedeelde `docs.rijksapp.nl`-certificaat blijven dat ook. Zestig opeenvolgende requests op `zad.rijksapp.nl` gaven zestig keer h2.

Tijdens de rolling restart zelf wisselde het antwoord nog tussen h2 en HTTP/1.1, afhankelijk van welke router-pod de verbinding kreeg. Dat trok binnen enkele minuten vanzelf recht.

De OpenShift-console valt hier buiten: die hangt aan de controller `apps` en blijft HTTP/1.1. De zes passthrough-routes gaan sowieso niet via ALPN op de router maar via de pod zelf.

### Werkt alles nog

| Controle | Uitkomst |
|---|---|
| Pods die herstartten na de wijziging | geen, op één bestaande CrashLoopBackOff van 17 uur oud na |
| Keycloak-log op fouten | alleen bestaande ruis uit de realm-sync, niets protocolgerelateerds |
| OIDC-redirectketen `zad.rijksapp.nl` naar `/introductie` | HTTP/2 200 |
| Discovery en JWKS op `keycloak.rijksapp.nl` | HTTP/2 200 |
| POST op het token-endpoint | HTTP/2 401 met `client_not_found`, dus het formulier komt correct aan |
| `SETTINGS_ENABLE_CONNECT_PROTOCOL` | 1, dus de router accepteert websockets over h2 (RFC 8441) |
| `SETTINGS_MAX_CONCURRENT_STREAMS` | 100, het getal uit punt 2 van de risico-afweging |

De verwachting uit punt 2 blijft daarmee staan: een browser kan nu tot honderd gelijktijdige streams over één verbinding openen waar dat er eerst zes waren. Er is nog geen backend gezien die daar last van heeft, maar het blijft het punt om op te letten bij een klacht over een trage of overbelaste applicatie.

### Wat het oplevert

Gemeten op de echte assetset van de editor van regelrecht, 29 bestanden, opgehaald zoals een browser dat doet:

| Manier van ophalen | Tijd |
|---|---|
| HTTP/1.1, maximaal 6 verbindingen | 0,35 s |
| HTTP/2, 1 gemultiplexte verbinding | 0,23 s |

Ongeveer 35% sneller, zo'n 120 ms. Zonder compressie, dus puur bandbreedtegebonden over 3,49 MB, is het 0,42 s tegen 0,34 s en dus een kleinere winst. Dat is de verwachte vorm: hoe minder bytes en hoe hoger de latency, hoe groter het aandeel van HTTP/2. Voor een gebruiker op een mobiele of tragere verbinding is de winst dus groter dan wat hier vanaf een kantoorlijn gemeten is.

Dat de winst op het externe stuk zit en niet op het interne, is te zien aan de latency aan beide kanten. Vanaf een werkplek naar de router is de TCP-connect 21 ms en is de TLS-handshake na 43 ms rond. Binnen de cluster is dat 3,8 ms naar de router-VIP en ongeveer 3 ms van pod naar pod. De limiet van zes verbindingen per host is bovendien een browserregel en geldt alleen op dat externe stuk; HAProxy kent richting de pod geen zes-verbindingenlimiet en houdt gewoon een pool van keep-alive-verbindingen aan.

Ter aanvulling op de tabel onder "Verhouding tot de andere maatregelen": regelrecht heeft zijn eigen drie maatregelen inmiddels doorgevoerd. Compressie staat aan (de hoofdbundel is 1,62 MB kaal, 417 KB met gzip en 326 KB met brotli), de assets dragen `cache-control: public, max-age=31536000, immutable`, en het aantal `modulepreload`-regels in de HTML is van 20 naar 4 gegaan. De initiële payload van de editor is daarmee 559 KiB gecomprimeerd, waar het in augustus nog 2626 KiB kaal was.

### Het pad vanuit de cluster

Applicaties in de cluster benaderen elkaar deels via de publieke hostname, bijvoorbeeld de operations manager die met `keycloak.rijksapp.nl` praat. Dat verkeer gaat nu ook over h2. Gemeten vanuit de pod `operations-manager` in `rig-prd-operations`:

- De cluster-DNS lost `keycloak.rijksapp.nl` op naar `router.rijksapp.nl`, dus naar 147.181.48.71 en `2a04:9a00:1007:4000:0:2:0:8`. Bij dual-stack wint IPv6 en landt de verbinding op het IPv6-adres.
- Het verkeer blijft binnen het clusternetwerk. Een TTL-meting geeft de router-VIP op 2 hops, een keycloak-pod rechtstreeks op 6 hops en een echt externe host (`rcr.rijksapps.nl`) op 8 hops. Er is dus geen lus langs een externe load balancer.
- TLS termineert op HAProxy. Richting de keycloak-pod blijft het cleartext HTTP/1.1, want de route is `edge`.
- Keycloak logt het pod-IPv6-adres van de aanroeper als client-IP, dus de `X-Forwarded-For` blijft over h2 intact.
- De operations manager zelf spreekt nog gewoon HTTP/1.1, omdat httpx alleen h2 doet met `http2=True`. Er verandert dus feitelijk niets aan dat verkeer, ook al staat h2 aan.
- De kortere weg `http://keycloak:8080` binnen de namespace bestaat en werkt, maar wordt niet gebruikt omdat de issuer-URL publiek moet zijn.

### Commando's om het te herhalen

```bash
curl -sS -o /dev/null -w "%{http_version}\n" https://zad.rijksapp.nl               # 2
curl -sS -o /dev/null -w "%{http_version}\n" https://editor.regelrecht.rijks.app   # 2
curl -sS -o /dev/null -w "%{http_version}\n" https://docs.rijksapp.nl              # 1.1, gedeeld cert
curl -sS -o /dev/null -w "%{http_version}\n" \
  https://profiel-test-mpfpsm-lcl.rig.prd1.gn2.quattro.rijksapps.nl                 # 1.1, wildcard cert
```

## HTTP/2 helemaal tot aan de pod

Terecht opgemerkt bij het lezen hiervan: met de default eindigt HTTP/2 op de router. Bij `edge` termineert HAProxy de TLS en gaat het verkeer als cleartext HTTP/1.1 verder naar de pod. Wie h2 tot in de pod wil, moet dus iets anders doen. Er zijn drie manieren, en passthrough is er daar één van.

**1. `reencrypt`.** De serverregel naar de backend krijgt `ssl` en, zolang HTTP/2 aanstaat, `alpn h2,http/1.1`. Uit `openshift/router`, `release-4.20`, `images/router/haproxy/conf/haproxy-config.template` regel 795:

```
server {{ $endpoint.ID }} {{ $endpoint.IP }}:{{ $endpoint.Port }} cookie ...
  {{- if (eq $cfg.TLSTermination "reencrypt") }} ssl
    {{- if not (isTrue $router_disable_http2) }} alpn h2,http/1.1
```

De pod moet dan zelf TLS aanbieden met een certificaat dat de router accepteert. De router blijft wel gewoon zijn werk doen: headers, sticky cookies, health checks.

**2. `edge` met `appProtocol: kubernetes.io/h2c` op de service-poort.** Dan zet de router `proto h2` op de serverregel en spreekt hij cleartext HTTP/2 tegen de pod. Regel 807 van dezelfde template:

```
{{- else if or (eq $cfg.TLSTermination "") (eq $cfg.TLSTermination "edge") }}
  {{- if or (eq $endpoint.AppProtocol "h2c") (eq $endpoint.AppProtocol "kubernetes.io/h2c") }} proto h2
```

Dit is de lichtste optie: geen extra certificaat, alleen een veld op de Service. De voorwaarde is dat de applicatieserver h2c spreekt. Uvicorn doet dat niet, hypercorn wel.

**3. `passthrough`.** De router raakt de TLS niet aan en de pod onderhandelt zelf met de browser, dus h2 van begin tot eind. De prijs is dat de pod een eigen geldig certificaat moet dragen en dat de router niets meer kan doen: geen `X-Forwarded-For`, geen padrouting, geen sticky cookies, geen health check op HTTP-niveau.

Voor ZAD is geen van de drie op dit moment nodig. De winst zit op het externe stuk tussen browser en router, en dat stuk spreekt nu h2. Het interne stuk heeft een RTT van enkele milliseconden en geen verbindingslimiet, dus daar valt met multiplexing vrijwel niets te winnen. De stand in productie op 4 september 2026 is 255 `edge`-routes, 6 `passthrough`, 2 zonder TLS en geen enkele `reencrypt`.

Wat het HTTP/1.1-stuk naar de pod wél betekent, is het spiegelbeeld van het risico uit punt 2 hierboven: de tot honderd streams die een browser over één h2-verbinding kan openen, waaiert HAProxy uit naar meer parallelle HTTP/1.1-verbindingen richting de pod. Dat is de plek om te kijken als een applicatie het na deze wijziging zwaarder krijgt.

## Bronnen

- [Enabling HTTP/2 Ingress connectivity, Red Hat documentatie](https://docs.redhat.com/en/documentation/openshift_container_platform/4.11/html/networking/configuring-ingress)
- [Is it possible to enable HTTP/2 for only individual routes, Red Hat solution 7124213](https://access.redhat.com/solutions/7124213)
- [cluster-ingress-operator, release-4.20](https://github.com/openshift/cluster-ingress-operator/blob/release-4.20/pkg/operator/controller/ingress/deployment.go)
- [openshift/router, release-4.20](https://github.com/openshift/router/blob/release-4.20/pkg/router/template/template_helper.go)
- [haproxy-config.template, release-4.20](https://github.com/openshift/router/blob/release-4.20/images/router/haproxy/conf/haproxy-config.template)
