### Een eigen domein naar ZAD laten wijzen

Stappenplan om een eigen domeinnaam (voorbeeld: `digitaledienst.overheid.nl`) te laten uitkomen op een applicatie op ZAD.

#### 1. DNS-record aanvragen

Aanvragen bij de (DNS) beheerder van het domein.

| Type            | Waarde                                                        |
| --------------- | ------------------------------------------------------------- |
| CNAME           | `router.rijksapp.nl.`                                         |
| ALIAS of ANAME  | `router.rijksapp.nl.`                                         |
| A + AAAA        | `147.181.48.71` en `2a04:9a00:1007:4000:0:2:0:8`               |

Aandachtspunten:

- Op een kaal domein is geen CNAME toegestaan; daar geldt ALIAS/ANAME of A + AAAA.
- Bij A en AAAA moeten beide records worden gezet, anders vervalt de IPv6-bereikbaarheid.
- Bij CNAME en ALIAS/ANAME volgt een wijziging van de IP-adressen van het cluster automatisch. Bij A en AAAA is dat handwerk aan de kant van de DNS beheerder.
- Voor elk subdomein is een eigen record nodig.

#### 2. Project in ZAD aanmaken

Maak een project aan, kies bij het webadres het eigen domein en vul expliciet het subdomein in. Domein en subdomein worden vastgelegd als aanvraag en door ZAD-beheer goedgekeurd. Daarna accepteert het platform verkeer voor die hostnaam.

#### 3. Certificaat

Standaard vraagt het platform een Let's Encrypt-certificaat aan en verlengt dat automatisch. Voorwaarden:

- de DNS staat al en werkt, voordat de deployment wordt uitgerold;
- een CAA-record op de eigen naam staat `letsencrypt.org` toe. Op `overheid.nl` is dat al het geval.

Een eigen certificaat (later) is ook mogelijk: aangeleverd in ZAD en aangeboden op de ingress, of doorgelaten tot in de pod (passthrough). Aanvraag van zo'n certificaat, bijvoorbeeld PKIoverheid, ligt bij de organisatie zelf; het staat op de eigen organisatiegegevens en ZAD kan het niet namens een ander aanvragen. Voor internet.nl is PKIoverheid niet vereist, een Let's Encrypt-certificaat scoort daar volledig.

#### 4. internet.nl

Het platform levert: doorverwijzing van HTTP naar HTTPS, HSTS (een jaar, includeSubDomains, preload), uitsluitend TLS 1.3, een publiek vertrouwd certificaat, IPv6 op de router en RPKI-geldige prefixes (`147.181.48.0/22` en `2a04:9a00:1007::/48`, AS202553).

Twee onderdelen blijven bij de organisatie:

- **DNSSEC.** `overheid.nl` is ondertekend, dus een record in die zone is gedekt. Bij delegatie naar een eigen nameserver moet de subzone zelf ondertekend zijn en hoort er een DS-record bij de delegatie.
- **Security headers in de applicatie.** De router zet alleen HSTS. `X-Content-Type-Options`, `X-Frame-Options` of `frame-ancestors`, `Referrer-Policy`, `Content-Security-Policy` en `Permissions-Policy` moet de applicatie zelf meesturen. Dit is in de praktijk het onderdeel waarop een dienst op ZAD nog zakt.
