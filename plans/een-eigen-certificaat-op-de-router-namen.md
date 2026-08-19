# Een eigen certificaat op de router-namen, zodat de sitetoets slaagt

De beheerder van het domeinbeleid wijst erop dat `router.rijksapp.nl` niet voldoet aan de verplichte standaarden uit Pas toe of leg uit, en kiest daarom losse A/AAAA-records in plaats van een CNAME naar onze naam. De mailhelft van dat bezwaar heeft een eigen plan en een branch die al klaar staat. Dit is de andere helft: de sitetoets.

## Wat er gemeten is (18 en 19 augustus 2026)

```
$ openssl s_client -connect router.rijksapp.nl:443 -servername router.rijksapp.nl
subject=CN=*.rig.prd1.gn2.quattro.rijksapps.nl
issuer=C=US, O=Let's Encrypt, CN=YE1

$ curl https://router.rijksapp.nl/
curl: (60) SSL: no alternative certificate subject name matches target host name

router.rijksapp.nl   A 147.181.48.71   AAAA 2a04:9a00:1007:4000:0:2:0:8   DNSSEC AD=true
router.rijksapp.nl   CAA -    (erft dus de apex)
rijksapp.nl (apex)   CAA 0 issue "letsencrypt.org", 0 issuewild "letsencrypt.org"

$ kubectl get ingress -A   ->   geen enkele Ingress met host router.*
```

De diagnose is dus niet "er zit niks achter", maar **het certificaat is voor een andere naam**. Omdat geen enkele Ingress deze host claimt, valt de OpenShift-router terug op zijn eigen wildcard van de ODCN-zone, en die matcht `router.rijksapp.nl` niet. Elke HTTPS-verbinding faalt op naamvalidatie, en daar zakt de toets op.

IPv6, DNSSEC en CAA zijn al in orde. Er is dus **geen DNS-wijziging nodig** om een certificaat te kunnen laten uitgeven.

## Wat er moet komen

Per beheerde zone (`rijksapp.nl`, `rijks.app`, `rijksapp.dev`) een Ingress op `router.<zone>` die:

- een Let's Encrypt-certificaat op precies die naam draagt;
- HTTP naar HTTPS stuurt;
- HSTS en de overige securityheaders zet;
- status 200 teruggeeft met een korte pagina die uitlegt wat deze naam is.

**De pagina is niet het werkzame deel, het certificaat is dat.** Bouw hier dus geen applicatie voor. Het kleinste dat 200 en de juiste headers kan geven volstaat. Of dat een statische pod wordt of de bestaande operations-manager die deze host meebedient, is een keuze die je met een reden mag maken; noem hem in de PR.

## Dat het hier hoort

| Onderdeel | Bestand |
|---|---|
| het model om te kopiëren | `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/ingress-rijksapp.yaml` |
| de headers die project-ingresses al zetten | `operations-manager/python/manifests/ingress.yaml.jinja` |
| welke zones we beheren | `opi/core/dns_config.py` (`MANAGED_DNS_ZONES`) |
| waar de A/AAAA vandaan komen | handmatig in TransIP, zie `docs/dns-router-zone-migration.md` |

`ingress-rijksapp.yaml` heeft alles wat je nodig hebt: de cert-manager-issuer, ECDSA-sleutel, het ip_whitelist en de HSTS-annotatie. Op één ding na, en dat is precies de valkuil hieronder.

## Valkuilen

**De zelfverwijzende CNAME. Dit is de gevaarlijke.** external-dns draait met `--policy=sync`. Het model dat je kopieert zet `external-dns.alpha.kubernetes.io/target: router.rijksapp.nl`, en op een Ingress voor die naam zelf betekent dat een CNAME van `router.rijksapp.nl` naar `router.rijksapp.nl`, bovenop de A/AAAA die er staan. Dat is een lus op precies de naam waar alle andere namen naartoe wijzen, dus dan is het hele platform onbereikbaar. Neem die annotatie niet over, en toets vóór het uitrollen dat external-dns deze Ingress met rust laat. `external-dns.alpha.kubernetes.io/exclude` wordt nergens in deze repo gebruikt, dus verifieer op de sandbox dat de annotatie in deze versie werkt in plaats van erop te vertrouwen.

Er is één beschermlaag die je niet als vangnet mag gebruiken: de bestaande A/AAAA zijn handmatig gezet en missen de TXT-eigendomsmarkering, en external-dns weigert records aan te raken die het niet bezit (`docs/dns-router-zone-migration.md`). Dat redt je waarschijnlijk als er toch iets misgaat, maar bouw er niet op.

**De securityheaders komen niet vanzelf mee.** Wat je op `zad.rijksapp.nl` ziet (CSP, X-Frame-Options, Referrer-Policy) komt uit de applicatie, niet uit de Ingress. Alleen HSTS zit in een annotatie. Een statische pagina moet de rest zelf zetten, anders zakt de sitetoets alsnog op headers in plaats van op het certificaat.

**Eén zone tegelijk.** De HTTP-01-challenge van cert-manager loopt over dezelfde Ingress die je aanmaakt. Rol `rijksapp.nl` uit, wacht op het certificaat, bevestig dat `zad.rijksapp.nl` het nog doet, en pas dan de volgende zone.

**Controleer per zone of de naam echt naar deze cluster wijst** voordat je hem claimt. `rijksapp.dev` wordt ook door de sandbox gebruikt; meet het in plaats van het aan te nemen.

## Wat hier NIET bij hoort

- **Het no-mail-beleid.** Dat is de andere helft van dezelfde feedback, met een eigen plan (`plans/geen-mail-vanaf-onze-dns-namen.md`) en een branch die al af is: `forgejo/een-no-mail-beleid-op-onze-dns-namen-langs-dezelfd`, twee commits, nul bestandsoverlap met main. Samen maken ze de mail- en de sitetoets groen, maar ship ze los.
- **Dat `rijksapp.nl` buiten AZ/DPC geregistreerd staat.** Organisatorisch. Geen certificaat en geen pagina repareert dat, en het bezwaar blijft staan als je doet alsof van wel.
- **De CAA per subdomein.** Staat al geparkeerd in het no-mail-plan.
- **Afspraken met partijen die losse A/AAAA naar ons laten wijzen.** Zij zetten onze IP's vast in hun zone en wij kunnen hun domein niet repareren als dat IP verandert. Operationeel, hoort in een afspraak, niet in deze taak.

## Verifieerbaar

- `openssl s_client -connect router.rijksapp.nl:443 -servername router.rijksapp.nl` geeft een certificaat waarvan de SAN `router.rijksapp.nl` bevat, en `curl https://router.rijksapp.nl/` geeft 200 zonder foutmelding 60.
- `dig @ns0.transip.net router.rijksapp.nl A` en `AAAA` geven nog steeds 147.181.48.71 en 2a04:9a00:1007:4000:0:2:0:8, en er is geen CNAME bijgekomen.
- De responseheaders bevatten HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy en een CSP met `frame-ancestors 'none'`.
- De internet.nl-sitetoets op `router.rijksapp.nl` vóór en ná, beide uitslagen in de PR.
- `zad.rijksapp.nl` en een willekeurig projectsubdomein blijven werken; het zijn CNAME's naar deze naam.
- Herhaald per zone, met dezelfde bewijzen.
