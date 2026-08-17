# CAA-records op onze eigen DNS-zones

## Wat het is

Zonder CAA mag elke publiek vertrouwde CA ter wereld een certificaat uitgeven voor elke
naam onder `rijks.app`, `rijksapp.nl` en `rijksapp.dev`. Een CAA-record in DNS zegt wie dat
mag. Wij gebruiken alleen Let's Encrypt, dus die grendel kan erop.

OPI zet die records zelf, bij elke start, via de TransIP API. Per beheerde zone komen er
twee records op de apex:

```
rijks.app.  3600  IN  CAA  0 issue     "letsencrypt.org"
rijks.app.  3600  IN  CAA  0 issuewild "letsencrypt.org"
```

`issuewild` gaat mee omdat de sandbox `*.sandbox.rijksapp.dev` van een wildcard serveert:
een zone met alleen `issue` verbiedt de volgende wildcard-uitgifte.

Er is bewust **geen** `iodef`. Dat record stuurt CA-misbruikmeldingen naar een mailbox, en
een onbemande mailbox daar is erger dan geen record.

## Hoe het werkt

| Onderdeel | Bestand |
|---|---|
| De gewenste toestand (zones, issuers, TTL) | `opi/core/dns_config.py` |
| De TransIP REST API v6 | `opi/connectors/transip.py` |
| De reconciler | `opi/core/caa_reconciler.py` |
| De aanroep bij het opstarten | `opi/core/startup.py` |

De reconciler is **add-only**:

1. Geen `TRANSIP_ACCOUNT_NAME` of `TRANSIP_PRIVATE_KEY`: loggen en klaar. Sandbox en local
   doen dus vanzelf niets.
2. `list_domains()` en de intersectie met `MANAGED_DNS_ZONES`. Een zone die we declareren
   maar het account niet houdt wordt een warning en verder niets - een tikfout in de lijst
   kan zo nooit de zone van iemand anders raken.
3. Per zone de CAA-records op de apex lezen, genormaliseerd vergelijken (TransIP kan
   quoting en witruimte anders teruggeven; naïef vergelijken zou bij elke start een
   duplicaat POST'en), en toevoegen wat mist.
4. Een CAA-record dat wij niet verwachten wordt gelogd en **blijft staan**. Zo'n record kan
   een bewuste uitzondering zijn tijdens een CA-migratie; automatisch wegpoetsen breekt
   iemands uitgifte zonder dat het opvalt. Opruimen is mensenwerk - de connector heeft
   daarom niet eens een verwijdermethode.

Alleen de apex (`@`), nooit een diepere naam: TransIP handhaaft RFC 1035 strikt en weigert
een record naast een CNAME. Nodig is het ook niet, want een CA klimt bij uitgifte omhoog tot
de eerste CAA-set die hij vindt.

Een gevolg van add-only: een bestaande TTL (op `rijksapp.nl` staat 86400) wordt niet naar
3600 gecorrigeerd. De inhoud klopt, en TTL-drift is de prijs voor een reconciler die nooit
iets weggooit.

De aanroep in `run_startup_tasks()` staat achter een `except Exception`. Dat is hier op zijn
plaats: publieke DNS mag de boot van het portaal niet tegenhouden, en elke herstart
repareert de drift alsnog.

## Configuratie

```
TRANSIP_ACCOUNT_NAME    TransIP-login
TRANSIP_PRIVATE_KEY     de hele PEM-sleutel
```

Het bestaan van deze twee is de aan/uit-knop. Op ODCN komen ze uit het bestaande secret
`transip-credentials` in `rig-prd-operations` (external-dns gebruikt datzelfde secret), via
`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`.

De TransIP-key is IP-gebonden: de calls slagen alleen vanuit het productiecluster. Lokaal
geeft de API 401.

## Een zone toevoegen

1. **Check eerst de CT-logs.** Alle publiek vertrouwde certificaten moeten sinds 2018 in
   Certificate Transparency staan, dus daar staat precies welke CA's er uitgeven voor die
   zone en zijn subdomeinen (bijvoorbeeld via de Cert Spotter API). Een CA die je vergeet
   betekent dat zijn vernieuwingen stil gaan falen - en dat merk je pas negentig dagen
   later.
2. Zet de zone erbij in `MANAGED_DNS_ZONES` in `opi/core/dns_config.py`, met de issuers die
   uit stap 1 komen.
3. Zorg dat het TransIP-account de zone ook echt houdt; anders logt OPI een warning en doet
   niets.

## Een tweede CA toestaan

1. Weer eerst de CT-logs, om dezelfde reden.
2. Zet de CA in `CAA_IDENTIFIERS` (naam -> de issuer-domain-name die die CA publiceert) en
   voeg hem toe aan de issuerlijst van de zone.

De test `test_every_nice_url_domain_under_managed_zone_uses_allowed_issuer` loopt alle
clusters in `CLUSTER_CONFIG` langs: elk `nice_url.supported_domains`-domein met een `issuer`
dat onder een beheerde zone valt, moet een issuer noemen die die zone toestaat. Dat is de
eigenlijke opbrengst van deze feature: het risico van CAA zit niet in het zetten, maar in
een dienst die later onder onze zone bij een andere CA gaat vernieuwen. Die fout wordt hier
een rode test op het moment dat het domein wordt toegevoegd.

## Terugdraaien

De records verwijderen; effectief binnen 8 uur (de Baseline Requirements laten een CA een
CAA-resultaat hergebruiken voor de TTL of 8 uur, wat langer is). Geen bestaand certificaat
gaat stuk: CAA wordt alleen bij uitgifte geraadpleegd, en cert-manager vernieuwt ruim een
maand voor het verlopen.

## Afhankelijkheden

- TransIP REST API v6 (`https://api.transip.nl/v6`), RSA-getekende auth
- Het secret `transip-credentials` in `rig-prd-operations`
- Egress naar TCP 443 (staat al toe in `operations-manager-policy`)

## Tests

```bash
cd operations-manager/python
uv run pytest tests/test_caa_config.py tests/test_caa_reconciler.py -q
```
