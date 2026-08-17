# Ship: CAA-records op onze eigen DNS-zones

**Status**: klaar om te bouwen, in één keer
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

Geen nieuw secret, geen SOPS-wijziging, geen netpol-wijziging, geen scheduler, geen feature flag. Zie "waarom dit zo weinig is" onderaan.

## 1. `opi/core/dns_config.py`

Cluster-onafhankelijk, want een DNS-zone is geen clustereigenschap. `sandbox.rijksapp.dev` staat op `sandboxed-local` en zit in de zone `rijksapp.dev` die `odcn-production` gebruikt: één zone, twee clusters. Daarom niet in `CLUSTER_CONFIG`.

```python
"""DNS zones we administer ourselves, independent of any cluster.

A zone is not a cluster property: ``sandbox.rijksapp.dev`` (sandboxed-local) lives
inside the ``rijksapp.dev`` zone that odcn-production uses, so a per-cluster home
would give one zone two conflicting policies. This module is the single source.
"""

# Zone apex at TransIP -> the issuers allowed to issue for anything in it.
# Issuer names are the same ones ``nice_url.supported_domains`` uses.
MANAGED_DNS_ZONES: dict[str, list[str]] = {
    "rijks.app": ["letsencrypt"],
    "rijksapp.nl": ["letsencrypt"],
    "rijksapp.dev": ["letsencrypt"],
}

# Issuer name -> the CAA issuer-domain-name that CA publishes.
CAA_IDENTIFIERS: dict[str, str] = {
    "letsencrypt": "letsencrypt.org",
}

# The Baseline Requirements let a CA reuse a CAA result for the TTL or 8 hours,
# whichever is greater. Below 8h buys nothing; above it only lengthens how long a
# mistake sticks around.
CAA_TTL = 3600


def desired_caa_contents(zone: str) -> list[str]:
    """CAA RDATA strings for a managed zone's apex, in a stable order."""
```

`issuewild` gaat mee, want de sandbox serveert `*.sandbox.rijksapp.dev` van een wildcard. Zonder `issuewild` breekt de volgende wildcard-uitgifte.

Geen `iodef`. Dat record stuurt CA-misbruikmeldingen naar een mailbox en een onbemande mailbox daar is erger dan geen record. internet.nl klaagt er niet over. Later één regel.

## 2. `opi/connectors/transip.py`

Conform het connectorpatroon: alle externe calls hier, nergens anders.

```python
class TransIPConnector:
    def __init__(self, account: str, private_key_pem: str) -> None: ...
    async def list_domains(self) -> list[str]: ...
    async def get_dns_entries(self, zone: str) -> list[dict[str, Any]]: ...
    async def add_dns_entry(self, zone: str, name: str, record_type: str, content: str, ttl: int) -> None: ...
```

**Geen delete-methode.** Die hebben we niet nodig, en wat er niet is kan ook niet per ongeluk gebruikt worden.

De auth-logica staat al werkend in `scripts/transip_delete_dns.py`: `POST /v6/auth` met een JSON-body, RSA PKCS1v15 over SHA-512 over **exact de verzonden bytes**, base64 in de `Signature`-header. Neem die body letterlijk over, inclusief `"global_key": True` en `"read_only": False`, want dat pad is bewezen tegen dit account. Bouw de body één keer en onderteken dezelfde bytes die je verstuurt, anders faalt de signature. Token cachen op de instance.

`base_url` is `https://api.transip.nl/v6`. Gebruik `HttpConnector` uit `opi/connectors/http.py` of `aiohttp` direct, wat schoner uitkomt.

## 3. `opi/core/config.py`

```python
    TRANSIP_ACCOUNT_NAME: str | None = None
    TRANSIP_PRIVATE_KEY: str | None = None  # PEM, de hele sleutel
```

Het bestaan hiervan is de aan/uit-knop. Geen credentials betekent overslaan, dus sandbox en local doen vanzelf niets.

## 4. `opi/core/caa_reconciler.py`

```python
async def reconcile_caa_records() -> None:
    """Ensure CAA records on every managed zone this TransIP account actually holds.

    Add-only: an unexpected CAA record is logged and left alone.
    """
```

Gedrag, in deze volgorde:

1. Geen `TRANSIP_ACCOUNT_NAME` of `TRANSIP_PRIVATE_KEY`: loggen op info en klaar.
2. `list_domains()`. Neem de intersectie met `MANAGED_DNS_ZONES`. Een zone die we declareren maar het account niet houdt, wordt een warning en verder niets. Dat is de grendel: een tikfout in `MANAGED_DNS_ZONES` kan nooit een zone van iemand anders raken.
3. Per zone: `get_dns_entries()`, filter op `type == "CAA"` en `name in ("@", "")`.
4. Vergelijk **genormaliseerd**, niet als kale string. TransIP kan quoting en witruimte anders teruggeven, en een naïeve vergelijking POST't dan bij elke start opnieuw een duplicaat. Normaliseer naar `(flags, tag.lower(), value.strip('"').lower())`.
5. Wat mist: `add_dns_entry(zone, "@", "CAA", content, CAA_TTL)`.
6. Wat er extra staat: `logger.warning`, nooit verwijderen. Een vreemd CAA-record kan een bewuste uitzondering zijn tijdens een CA-migratie, en dat automatisch wegpoetsen breekt iemands uitgifte zonder dat het opvalt. Opruimen is mensenwerk.

Alleen `@`, nooit een diepere naam: TransIP handhaaft RFC 1035 strikt en weigert een record naast een CNAME (daarom bestaat `--txt-prefix=edns-` al bij external-dns). Niet nodig ook, want een CA klimt bij uitgifte omhoog tot de eerste CAA-set die hij vindt.

Add-only betekent ook dat de bestaande TTL van 86400 op `rijksapp.nl` niet wordt gecorrigeerd naar 3600. Dat laten we zo: de inhoud klopt, en TTL-drift is de prijs voor een reconciler die nooit iets weggooit.

## 5. `opi/core/startup.py`

Eén aanroep in `run_startup_tasks()`, na de kritieke fases, non-blocking:

```python
    try:
        await reconcile_caa_records()
    except Exception as e:  # non-critical: DNS hygiene must never block boot
        logger.error("CAA reconciliation failed: %s", e)
```

Dit is de ene plek waar een kale `except Exception` op zijn plaats is, en met die reden erbij: publieke DNS mag de boot van het portaal niet tegenhouden. Elke herstart repareert drift, dus een gemiste ronde kost niets.

## 6. De env-vars in de odcn-overlay

In `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`, bij de bestaande env-lijst:

```yaml
        - name: TRANSIP_ACCOUNT_NAME
          valueFrom:
            secretKeyRef:
              name: transip-credentials
              key: TRANSIP_ACCOUNT_NAME
        - name: TRANSIP_PRIVATE_KEY
          valueFrom:
            secretKeyRef:
              name: transip-credentials
              key: TRANSIP_PRIVATE_KEY
```

Dat secret bestaat al in `rig-prd-operations`, want external-dns gebruikt het daar.

## 7. Tests

`tests/test_caa_config.py`

- `test_desired_contents_per_zone`: drie zones, elk exact `['0 issue "letsencrypt.org"', '0 issuewild "letsencrypt.org"']`
- `test_every_nice_url_domain_under_managed_zone_uses_allowed_issuer`: loop over **alle** clusters in `CLUSTER_CONFIG`. Voor elke `supported_domains`-entry die een `issuer` heeft en die gelijk is aan of eindigt op een beheerde zone, moet die issuer in de lijst van die zone staan. Entries zonder issuer overslaan (`sandbox.rijksapp.dev`, `kind`, `local`), entries buiten elke beheerde zone overslaan (`robbertuittenbroek.nl`).

Die tweede test is de eigenlijke opbrengst van dit ship. Het risico bij CAA is niet het zetten, het is dat iemand later een dienst onder onze zone bij een andere CA laat vernieuwen en dat pas merkt als de vernieuwing stil faalt. Die fout wordt hier een rode test op het moment dat het domein wordt toegevoegd, in plaats van een storing negentig dagen later.

`tests/test_caa_reconciler.py`, tegen een gemockte connector, geen echte calls:

- `test_no_credentials_skips`: nul calls
- `test_zone_not_in_account_is_skipped`: `list_domains()` zonder `rijks.app` geeft een warning en nul adds op die zone
- `test_correct_zone_does_nothing`: nul adds
- `test_quoting_variants_count_as_present`: `0 issue "LETSENCRYPT.ORG"` en `0  issue  "letsencrypt.org"` gelden als aanwezig, nul adds
- `test_empty_zone_adds_two`: twee adds, `name="@"`, `ttl=3600`
- `test_unexpected_caa_is_warned_not_deleted`: een CAA voor een andere CA geeft een warning, nul deletes, en de eigen records worden alsnog toegevoegd

## 8. `features/caa-records.md`

Wat het is, waar de config staat, hoe je een zone toevoegt, hoe je een tweede CA toestaat, en de regel die daarbij hoort: **check eerst de CT-logs** voordat je een CA toevoegt of een zone opneemt, want een CA die je vergeet betekent dat zijn vernieuwingen stil gaan falen.

## Klaar als

```bash
cd operations-manager/python
uv run ruff check . --fix && uv run ruff format . && uv run pyright
uv run pytest tests/test_caa_config.py tests/test_caa_reconciler.py -q
```

```bash
SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)" kustomize build \
  --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone \
  bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production | grep -B2 -A4 TRANSIP
```

Alles groen, `pyright` schoon, en de twee env-vars staan in de gerenderde deployment.

## Wat de bouw niet doet

**Niet uitrollen.** De reconciler schrijft bij de eerste start op productie in publieke DNS. Dat is een aparte, expliciete stap en die vraagt akkoord, conform de afspraak dat OPI niet wordt uitgerold tenzij gevraagd.

**Niets aan rijksapps.nl.** Dat is de zone van ODC-Noord. Onze CT-sweep laat zien dat ook daar alles Let's Encrypt is, dus zij kunnen hetzelfde record veilig zetten, maar dat is hun beslissing.

**Niets aan external-dns.** Dat kan geen CAA en gaat het niet leren.

## Handmatig na de uitrol, als het akkoord er is

1. OPI herstarten en de log nakijken. Verwachting: drie zones gevonden, twee adds op `rijks.app`, twee op `rijksapp.dev`, **nul** op `rijksapp.nl`, want die heeft de records al.
2. `dig CAA rijks.app`, `dig CAA rijksapp.nl`, `dig CAA rijksapp.dev` geven elk issue plus issuewild.
3. Eén certificaatvernieuwing forceren op een nice-url en zien dat Let's Encrypt gewoon uitgeeft. Verify in de CT-logs op een `not_before` na het zetten.

**Terugdraaien**: de records verwijderen, effectief binnen 8 uur. Geen bestaand certificaat gaat stuk, want CAA wordt alleen bij uitgifte geraadpleegd, en cert-manager vernieuwt ruim een maand voor het verlopen. Er is dus een maand marge voordat een fout iets zou omleggen.

## Waarom dit zo weinig is

Vier dingen bleken al te staan:

- **Het secret.** external-dns draait op ODCN in `rig-prd-operations`, dezelfde namespace als OPI, dus `transip-credentials` is er al.
- **Het netwerkpad.** `operations-manager-policy` staat egress toe naar `to: []` op TCP 443.
- **De IP-whitelist.** De TransIP-key is IP-gebonden (lokaal geeft 401), maar external-dns praat er vanuit deze namespace al mee. Zelfde egress-IP.
- **De auth-code.** `scripts/transip_delete_dns.py` doet de RSA-signed auth al.

En de CA-inventarisatie is af, gemeten 17 augustus 2026 via de Cert Spotter API over de CT-logs, inclusief subdomeinen: `rijks.app` 38 certificaten, `rijksapp.nl` 54, `rijksapp.dev` 23, en alle 115 van Let's Encrypt. Geen enkele andere CA, ook niet historisch. Alle publiek vertrouwde certificaten moeten sinds 2018 in CT-logs staan, dus dat is sluitend voor precies de CA's die zich aan CAA moeten houden. `issue "letsencrypt.org"` breekt niets.

`rijksapp.nl` heeft de twee records al staan (TTL 86400), `rijks.app` en `rijksapp.dev` hebben niets.
