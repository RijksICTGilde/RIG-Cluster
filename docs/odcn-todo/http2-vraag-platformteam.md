# Overleg met het platformteam: HTTP/2 op de ingress

**Datum**: 2026-08-03, bijgewerkt 2026-08-05
**Van**: RIG / ZAD
**Betreft**: IngressController `rig` op `prd1.gn2.quattro.rijksapps.nl`
**Onderbouwing**: [http2-ingress.md](http2-ingress.md)
**Status**: platformteam heeft gereageerd, wacht op afspraak over een testmoment

Dit document houdt de uitwisseling bij. Het onderste blok is het antwoord dat verstuurd kan worden.

## Wat wij hebben gevraagd

Of HTTP/2 aangezet kan worden op de IngressController die onze routes bedient, met de annotatie `ingress.operator.openshift.io/default-enable-http2=true`, en of het eerder is afgewogen.

Aanleiding: op alle ZAD-hostnames komt ALPN niet tot een afspraak. Gemeten op negen hostnames verspreid over meerdere projecten, overal HTTP/1.1, ook op de 57 routes met een eigen certificaat die aan alle HAProxy-voorwaarden voldoen. Het is dus geen eigenschap van onze routes, en OpenShift kent geen route-annotatie waarmee wij het zelf zouden kunnen aanzetten.

## Reactie van het platformteam

Samengevat:

- Het kan, hoofdstuk 8.9.16 van de documentatie.
- Aanzetten kan op ingresscontroller-niveau, maar geldt dan meteen voor alle routes van RIG. Dus alleen voor ons, niet platformbreed.
- Vraag terug: wat is daarvan het effect, en kan het toestaan van HTTP/2 ook negatief uitpakken?

## Ons antwoord

### Over de reikwijdte

De annotatie staat inderdaad op controllerniveau, maar de router beperkt het effect vervolgens zelf. Alleen routes met een eigen certificaat dat door precies één route wordt gebruikt, krijgen ALPN. Alles op het default wildcard-certificaat hangt aan een bind met een expliciete `no-alpn` en blijft onder alle omstandigheden HTTP/1.1.

Op de huidige toestand van het cluster betekent dat 49 van onze 187 routes, ongeveer een kwart. De circa 130 PR-preview-routes en 8 routes op een gedeeld certificaat veranderen niet. Die grens wordt door de router afgedwongen, niet door ons, en hij faalt veilig terug naar HTTP/1.1 als er ooit twee routes met identieke certificaten ontstaan.

### Kan het negatief werken? Vier punten

**1. De router herstart.** Het enige punt dat álle routes op `rig` raakt, ook de 138 die op HTTP/1.1 blijven. De annotatie wijzigt `ROUTER_DISABLE_HTTP2` in de router-deployment en dat geeft een rolling restart van de router-pods. Met meerdere replicas geen downtime, maar het verdient een gepland moment.

**2. Meer gelijktijdige requests op de backends.** Dit is wat ons betreft de realistische. De limiet van zes verbindingen per host in HTTP/1.1 werkt feitelijk als rate limiter. Over HTTP/2 kan één browser tot ongeveer 100 gelijktijdige streams op één verbinding openen, dus backends krijgen een hardere burst. Mocht dat ergens knellen, dan is er een per-route rem: `haproxy.router.openshift.io/pod-concurrent-connections`.

**3. WebSockets.** Over h2 gebruiken clients extended CONNECT (RFC 8441) in plaats van een HTTP/1.1 Upgrade. De router-image draait HAProxy 2.8 en ondersteunt dat, maar het is het enige functionele pad dat echt anders loopt. Van de 49 kandidaten zijn Grafana, Grist en OpenProject de voor de hand liggende om te testen.

**4. Reencrypt-routes.** Daar biedt de router ook `alpn h2,http/1.1` richting de pod aan, dus daar verandert iets aan de backendkant. Valt terug als de pod geen h2 adverteert, dus laag risico. Bij edge-routes gebeurt dit niet en blijft het cleartext HTTP/1.1 naar de pod.

**Terugdraaien is triviaal**: annotatie op `false` of weghalen, router herlaadt. Dat maakt het een goed omkeerbaar experiment.

### Wat wij er zelf bij vinden

Wij hebben de aanleidinggevende applicatie doorgemeten en willen het volgende niet onthouden: HTTP/2 is daar niet de grootste winst. De webserver van dat project comprimeert niets (2626 KiB die 723 KiB had kunnen zijn) en stuurt geen cache-headers. Die twee samen leveren meer op dan HTTP/2, en liggen bij het project zelf. Dat is inmiddels bij hen belegd.

Wij vragen HTTP/2 daarom niet als redding van één project, maar omdat het de juiste default is voor de controller en omdat elk asset-zwaar project op dit platform er baat bij heeft.

## Openstaande vragen aan het platformteam

1. Staat HTTP/2 uit op de IngressController `rig` of clusterbreed op `ingresses.config/cluster`? Wij kunnen dat zelf niet zien, beide geven ons `Forbidden`.
2. Is het eerder overwogen en bewust afgewezen? Zo ja, om welke reden, dan adresseren wij die.
3. Kan er een testmoment worden afgesproken, samen met de rolling restart van de router? Let op dat dit niet op de PR-preview-hostnames te testen is: die draaien op het default certificaat en krijgen daarom sowieso geen ALPN. Verificatie vereist een hostname met een eigen certificaat, bijvoorbeeld onder `*.regelrecht.rijks.app`.

## Verificatie achteraf

```bash
curl -sS -o /dev/null -w "%{http_version}\n" https://editor.regelrecht.rijks.app
```

Verwacht `2` op routes met een eigen certificaat, en ongewijzigd `1.1` op de preview-routes.

## Contact

RIG / ZAD, namens project regelrecht (`rig-prd-regel-k4c`).
