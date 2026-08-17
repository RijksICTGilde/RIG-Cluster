# Ship: CAA-records op onze eigen DNS-zones

**Status**: klaar om te bouwen, in een keer
**Datum**: 2026-08-17
**Aanleiding**: melding van Eelco over internet.nl, en de vraag of dit handwerk in het TransIP-portaal is of via de API kan
**Antwoord op die vraag**: external-dns kan geen CAA, de TransIP API wel, en de credentials staan al in de namespace van OPI

Zonder CAA mag elke publiek vertrouwde CA ter wereld een certificaat uitgeven voor elke naam onder `rijks.app`, `rijksapp.nl` en `rijksapp.dev`. Met CAA staat in DNS wie dat mag. Wij gebruiken alleen Let's Encrypt, dus die grendel kan erop.

## De hele opdracht

| Bestand | Actie |
|---|---|
| `opi/core/dns_config.py` | nieuw, de gewenste toestand |
| `opi/connectors/transip.py` | nieuw, TransIP REST API v6 |
| `opi/core/caa_reconciler.py` | nieuw, add-only reconciler |
| `opi/core/config.py` | 2 settings erbij |
| `opi/core/startup.py` | 1 aanroep erbij |
| `bootstrap/.../overlays/odcn-production/patches/deployment.yaml` | 2 env-vars erbij |
| `tests/test_caa_config.py`, `tests/test_caa_reconciler.py` | nieuw |
| `features/caa-records.md` | nieuw |

Geen nieuw secret, geen SOPS-wijziging, geen netpol-wijziging, geen scheduler, geen feature flag.

## 1. `opi/core/dns_config.py`

Cluster-onafhankelijk, want een DNS-zone is geen clustereigenschap. `sandbox.rijksapp.dev` staat op `sandboxed-local` en zit in de zone `rijksapp.dev` die `odcn-production` gebruikt: een zone, twee clusters. Daarom niet in `CLUSTER_CONFIG`.

- `MANAGED_DNS_ZONES`: zone-apex -> toegestane issuers (`rijks.app`, `rijksapp.nl`, `rijksapp.dev`, elk `["letsencrypt"]`).
- `CAA_IDENTIFIERS`: issuernaam -> issuer-domain-name (`letsencrypt` -> `letsencrypt.org`).
- `CAA_TTL = 3600`. De Baseline Requirements laten een CA een CAA-resultaat hergebruiken voor de TTL of 8 uur, wat langer is; lager koopt niets, hoger verlengt alleen hoe lang een fout blijft plakken.
- `desired_caa_contents(zone)`: de CAA-RDATA-strings voor de apex, in vaste volgorde.

`issuewild` gaat mee, want de sandbox serveert `*.sandbox.rijksapp.dev` van een wildcard. Zonder `issuewild` breekt de volgende wildcard-uitgifte.

Geen `iodef`. Dat record stuurt CA-misbruikmeldingen naar een mailbox en een onbemande mailbox daar is erger dan geen record. internet.nl klaagt er niet over.

## 2. `opi/connectors/transip.py`

Conform het connectorpatroon: alle externe calls hier, nergens anders. `list_domains()`, `get_dns_entries(zone)`, `add_dns_entry(zone, name, record_type, content, ttl)`.

**Geen delete-methode.** Die hebben we niet nodig, en wat er niet is kan ook niet per ongeluk gebruikt worden.

De auth-logica staat al werkend in `scripts/transip_delete_dns.py`: `POST /v6/auth` met een JSON-body, RSA PKCS1v15 over SHA-512 over **exact de verzonden bytes**, base64 in de `Signature`-header, inclusief `"global_key": True` en `"read_only": False`. Bouw de body een keer en onderteken dezelfde bytes die je verstuurt. Token cachen op de instance. `base_url` is `https://api.transip.nl/v6`.

## 3. `opi/core/config.py`

`TRANSIP_ACCOUNT_NAME` en `TRANSIP_PRIVATE_KEY` (PEM, de hele sleutel), beide `str | None = None`. Het bestaan hiervan is de aan/uit-knop: geen credentials betekent overslaan, dus sandbox en local doen vanzelf niets.

## 4. `opi/core/caa_reconciler.py`

`reconcile_caa_records()`, in deze volgorde:

1. Geen credentials: loggen op info en klaar.
2. `list_domains()`, intersectie met `MANAGED_DNS_ZONES`. Een zone die we declareren maar het account niet houdt wordt een warning. Dat is de grendel: een tikfout kan nooit een zone van iemand anders raken.
3. Per zone `get_dns_entries()`, filter op `type == "CAA"` en `name in ("@", "")`.
4. Vergelijk **genormaliseerd** naar `(flags, tag.lower(), value.strip('"').lower())`. TransIP kan quoting en witruimte anders teruggeven; naief vergelijken POST't dan bij elke start een duplicaat.
5. Wat mist: `add_dns_entry(zone, "@", "CAA", content, CAA_TTL)`.
6. Wat er extra staat: `logger.warning`, nooit verwijderen. Opruimen is mensenwerk.

Alleen `@`, nooit een diepere naam: TransIP handhaaft RFC 1035 strikt en weigert een record naast een CNAME. Niet nodig ook, want een CA klimt bij uitgifte omhoog tot de eerste CAA-set die hij vindt.

Add-only betekent ook dat de bestaande TTL van 86400 op `rijksapp.nl` niet wordt gecorrigeerd. TTL-drift is de prijs voor een reconciler die nooit iets weggooit.

## 5. `opi/core/startup.py`

Een aanroep in `run_startup_tasks()`, na de kritieke fases, non-blocking, achter een kale `except Exception` met die reden erbij: publieke DNS mag de boot van het portaal niet tegenhouden. Elke herstart repareert drift.

## 6. De env-vars in de odcn-overlay

`TRANSIP_ACCOUNT_NAME` en `TRANSIP_PRIVATE_KEY` via `secretKeyRef` naar `transip-credentials`. Dat secret bestaat al in `rig-prd-operations`, want external-dns gebruikt het daar.

## 7. Tests

`tests/test_caa_config.py`

- `test_desired_contents_per_zone`: drie zones, elk exact `['0 issue "letsencrypt.org"', '0 issuewild "letsencrypt.org"']`
- `test_every_nice_url_domain_under_managed_zone_uses_allowed_issuer`: loop over **alle** clusters in `CLUSTER_CONFIG`; elke `supported_domains`-entry met een `issuer` die onder een beheerde zone valt, moet een issuer noemen die die zone toestaat.

Die tweede test is de eigenlijke opbrengst: het risico bij CAA is niet het zetten, het is dat iemand later een dienst onder onze zone bij een andere CA laat vernieuwen en dat pas merkt als de vernieuwing stil faalt.

`tests/test_caa_reconciler.py`, tegen een gemockte connector: geen credentials, zone niet in het account, correcte zone, quoting-varianten, lege zone, en een onverwacht CAA-record dat blijft staan.

## 8. `features/caa-records.md`

Wat het is, waar de config staat, hoe je een zone toevoegt, hoe je een tweede CA toestaat, en de regel die daarbij hoort: **check eerst de CT-logs**, want een CA die je vergeet betekent dat zijn vernieuwingen stil gaan falen.

## Wat de bouw niet doet

**Niet uitrollen.** De reconciler schrijft bij de eerste start op productie in publieke DNS; dat is een aparte, expliciete stap met akkoord.

**Niets aan rijksapps.nl** (de zone van ODC-Noord) en **niets aan external-dns** (dat kan geen CAA).

## Handmatig na de uitrol, als het akkoord er is

1. OPI herstarten en de log nakijken: drie zones gevonden, twee adds op `rijks.app`, twee op `rijksapp.dev`, nul op `rijksapp.nl`.
2. `dig CAA` op elke zone geeft issue plus issuewild.
3. Een certificaatvernieuwing forceren op een nice-url en in de CT-logs verifieren.

**Terugdraaien**: de records verwijderen, effectief binnen 8 uur. Geen bestaand certificaat gaat stuk, want CAA wordt alleen bij uitgifte geraadpleegd.

## Waarom dit zo weinig is

Het secret, het netwerkpad (`operations-manager-policy` staat egress toe naar `to: []` op TCP 443), de IP-whitelist en de auth-code stonden al. En de CA-inventarisatie is af: gemeten 17 augustus 2026 via de Cert Spotter API over de CT-logs, inclusief subdomeinen: `rijks.app` 38 certificaten, `rijksapp.nl` 54, `rijksapp.dev` 23, alle 115 van Let's Encrypt. `rijksapp.nl` heeft de twee records al staan (TTL 86400), `rijks.app` en `rijksapp.dev` hebben niets.
