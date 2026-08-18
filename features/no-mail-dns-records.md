# No-mail-records op onze eigen DNS-namen

## Wat het is

`router.rijksapp.nl` en zijn twee broertjes zijn kale A/AAAA-namen voor het cluster. Er komt
nooit mail vandaan en er hoort nooit mail voor aan te komen, maar zonder records die dat
zeggen heeft een ontvanger niets om op te toetsen - en daar valt de mailtoets uit Pas toe of
leg uit op.

**SPF erft niet naar subdomeinen.** Een ontvanger die `router.rijksapp.nl` toetst leest het
TXT-record van díe naam, niet dat van de apex. En de `sp=reject` op de apex geldt voor
subdomeinen zonder eigen beleid, maar de mailtoets vraagt om een record op de naam zelf.
Het beleid moet dus op elke naam staan.

Per gedeclareerde naam komen er drie records:

```
router.rijksapp.nl.          3600  IN  TXT  "v=spf1 -all"
router.rijksapp.nl.          3600  IN  MX   0 .
_dmarc.router.rijksapp.nl.   3600  IN  TXT  "v=DMARC1; p=reject;"
```

`v=spf1 -all` machtigt niemand om namens de naam te mailen, `0 .` is het null-MX van RFC
7505 (deze naam neemt geen mail aan) en de DMARC-regel zegt wat de ontvanger met een
vervalsing moet doen: weigeren.

## Welke namen

De lijst staat expliciet in `opi/core/dns_config.py` (`NO_MAIL_NAMES`), per beheerde zone,
met relatieve namen zoals TransIP ze schrijft. Gedeclareerd in plaats van afgeleid: een
lijst die je kunt lezen is hier meer waard dan een regel die slim is.

Er staat alleen `router` in, per zone, en dat is met reden:

- alles wat wij verder onder deze zones publiceren (`zad`, `keycloak`, projectsubdomeinen)
  is een **CNAME** naar zo'n router, en TransIP weigert elk record naast een CNAME. Gemeten
  op 18 augustus 2026: `zad.rijksapp.nl` en `keycloak.rijksapp.nl` zijn CNAME's naar
  `router.rijksapp.nl`;
- de drie **apexen** dragen `v=spf1 -all` en `v=DMARC1; p=reject; sp=reject` al. Een null MX
  daar zou zeggen dat de héle zone geen mail aanneemt, en dat is een beleidskeuze over de
  zone, geen hygiene-reparatie. De reconciler raakt de apex daarom niet aan.

## Hoe het werkt

| Onderdeel | Bestand |
|---|---|
| De gewenste toestand (namen, inhoud, TTL) | `opi/core/dns_config.py` |
| De TransIP REST API v6 | `opi/connectors/transip.py` |
| De reconciler | `opi/core/no_mail_reconciler.py` |
| De aanroep bij het opstarten | `opi/core/startup.py` (fase 7) |

Een eigen reconciler naast die van CAA, niet een tweede systeem: hij gebruikt dezelfde
connector en dezelfde twee poorten. De records, de vergelijking en de vraag "ontvangt deze
naam mail" hebben niets met certificaatuitgifte te maken; die twee in één functie proppen
zou één functie twee beleidsregels laten dragen.

De poorten en de gedragsregels:

1. Geen `TRANSIP_ACCOUNT_NAME` of `TRANSIP_PRIVATE_KEY`: loggen en klaar. Sandbox en local
   doen dus vanzelf niets, en OPI start gewoon door.
2. `list_domains()` en de intersectie met `MANAGED_DNS_ZONES`. Een zone die we declareren
   maar het account niet houdt wordt een warning en verder niets.
3. Per zone worden de DNS-entries één keer gelezen en per naam vergeleken, **genormaliseerd**:
   quoting, witruimte, hoofdletters en de punt aan het eind van een MX-doel mogen anders
   terugkomen dan wij ze stuurden. Zonder die normalisatie voegt elke start een duplicaat
   toe - de fout die ongemerkt kan uitgroeien.
4. **Add-only.** Een bestaand SPF-, MX- of DMARC-record wordt nooit vervangen. Hier telt dat
   dubbel: een tweede SPF-record maakt de evaluatie een permerror en een tweede DMARC-record
   laat DMARC de naam overslaan - beide maken het beleid ongeldig in plaats van strenger.
   Wijkt een bestaand record af van wat wij willen, dan is het een logregel en mensenwerk.
5. **Een naam die wel mail ontvangt krijgt nooit een null MX.** Staat er al een MX die niet
   het null-MX is, dan wordt die naam overgeslagen met een logregel.
6. Staat er een CNAME op de naam, dan wordt hij overgeslagen: TransIP weigert daar elk
   record naast.

De aanroep in `run_startup_tasks()` staat, net als die van CAA, achter een `except
Exception`: publieke DNS mag de boot van het portaal niet tegenhouden, en elke herstart
repareert de drift alsnog.

## Configuratie

Dezelfde twee variabelen als CAA, uit hetzelfde secret `transip-credentials` in
`rig-prd-operations`:

```
TRANSIP_ACCOUNT_NAME    TransIP-login
TRANSIP_PRIVATE_KEY     de hele PEM-sleutel
```

De TransIP-key is IP-gebonden: de calls slagen alleen vanuit het productiecluster.

## Een naam toevoegen

1. Controleer dat de naam **geen CNAME** is en **geen mail ontvangt** (`dig MX <naam>`).
   Doet hij dat wel, dan hoort hij hier niet.
2. Zet de relatieve naam erbij in `NO_MAIL_NAMES` in `opi/core/dns_config.py`.
3. De eerstvolgende start van OPI op productie zet de drie records.

## Terugdraaien

De records verwijderen. Er gaat niets stuk aan onze kant: er wordt vanaf deze namen geen
mail verstuurd en er wordt geen mail voor aangenomen. De mailtoets valt dan weer om.

## Afhankelijkheden

- TransIP REST API v6 (`https://api.transip.nl/v6`), RSA-getekende auth
- Het secret `transip-credentials` in `rig-prd-operations`
- Egress naar TCP 443

## Tests

```bash
cd operations-manager/python
uv run pytest tests/test_no_mail_config.py tests/test_no_mail_reconciler.py -q
```
