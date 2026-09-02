# Het domeinenblok heet niet meer naar een verdwenen mode

Het blok `nice_url` in `opi/core/cluster_config.py` heet naar `domain-mode: nice-url`, en die mode bestaat niet meer. De v2.8-migratie zet hem om naar `domain-format` en gooit de sleutel weg; de code noemt hem overal "the retired `domain-mode`" en "legacy". Het blok zelf is springlevend: het is de enige plek waar staat welke domeinen een cluster aanbiedt.

Deze wijziging hernoemt het blok en de functies eromheen. **Er verandert geen gedrag.** Geen migratie, geen schemawijziging, geen nieuwe of vervallen configuratie.

## Waarom

De naam kost tijd. Wie hem tegenkomt denkt naar legacy te kijken en gaat uitzoeken of het weg kan, terwijl het de huidige weg is. Dat is in deze codebase al minstens één keer gebeurd en het is een voorspelbare herhaling: de naam zegt iets anders dan de inhoud.

De inhoud is: per domein dat het cluster aanbiedt staat er of het punten toestaat (`supports_dots`), welke issuer erbij hoort (`issuer`), of subdomeinen beperkt zijn (`restricted_subdomains`) en waar external-dns naartoe moet wijzen (`external_dns_target`). Dat is een domeinenlijst, geen mode.

Wat `nice_url` ooit betekende leeft door als het veld `supports_dots`: dat bepaalt of een gebruiker de punt-varianten van `domain-format` te zien krijgt (`DomainFormatOptionsProvider.get_options`, `opi/forms/visualizers/providers.py:870`). Die vlag blijft heten zoals hij heet; alleen het blok eromheen krijgt een naam die klopt.

## Wat er nu is

Gemeten op 29-08-2026 tegen `main`.

Het blok staat in `opi/core/cluster_config.py` (27 verwijzingen, inclusief de blokken van alle clusters) met deze vorm:

```python
"nice_url": {
    "supported_domains": [
        {
            "domain": "fundament-poc.rijksapp.dev",
            "supports_dots": False,
            "issuer": "letsencrypt",
            "restricted_subdomains": True,
            "external_dns_target": "router.fundament-poc.rijksapp.dev",
        },
    ],
},
```

Vier functies dragen de naam, met hun aantal aanroepen in `opi/` en `tests/` samen:

| Functie | Plek | Aanroepen |
|---|---|---|
| `get_nice_url_config` | `cluster_config.py:1157` | 14 |
| `get_nice_url_supported_domains` | `cluster_config.py:1180` | 14 |
| `is_nice_url_domain_supported` | `cluster_config.py:1204` | 11 |
| `generate_nice_url_root_hostname` | `naming.py:1772` | 11 |

Lezers buiten `cluster_config.py`: `opi/utils/naming.py` (5), `opi/connectors/subdomain.py` (4), `opi/web/router_self_service.py` (2), `opi/manager/project_manager.py` (2), `opi/forms/visualizers/providers.py` (2), `opi/core/dns_config.py` (1).

Tests: acht bestanden, waarvan `tests/test_nice_url_naming.py` (28) en `tests/test_cluster_config_extended.py` (13) de zwaarste zijn.

**Buiten Python komt de naam niet voor als veld.** In `publish-on-web.deployment.v1.0.json` staat hij alleen in een beschrijvende tekst over de verdwenen `domain-mode`, en in `opi/schemas/` staat hij helemaal niet. Er is dus geen projectbestand dat hem draagt en geen migratie nodig.

## Naamkeuze

Vastgesteld op 29-08-2026, niet meer open.

`nice_url` wordt **`domains`**, met `supported_domains` eronder in de vorm die hij nu heeft. De nesting blijft dus; de inhoud van de lijst verandert niet.

```python
"domains": {
    "supported_domains": [
        {"domain": "...", "supports_dots": False, "issuer": "letsencrypt", ...},
    ],
},
```

De functies:

| Nu | Wordt |
|---|---|
| `get_nice_url_config` | `get_domains_config` |
| `get_nice_url_supported_domains` | `get_supported_domain_names` |
| `is_nice_url_domain_supported` | `is_domain_supported` |
| `generate_nice_url_root_hostname` | `generate_root_hostname` |

Let op bij de tweede: er bestaat al `get_supported_base_domains` in `opi/connectors/subdomain.py:153`, met een andere betekenis (die verzamelt over alle clusters). Daarom `get_supported_domain_names` en niet `get_supported_domains`; die twee zouden anders onleesbaar dicht bij elkaar komen.

## Taken

### 1. Het blok hernoemen in `cluster_config.py`

Pas de gekozen vorm toe op alle clusterblokken en op de functies die het blok lezen. `get_domain_issuer`, `get_domain_supports_dots`, `is_domain_subdomain_restricted`, `get_restricted_subdomain_domains` en `get_external_dns_target_for_hostname` lezen het blok rechtstreeks en moeten mee.

Verify: `grep -c nice_url opi/core/cluster_config.py` geeft 0.

### 2. De vier functies hernoemen en hun aanroepers meenemen

Geen aliassen achterlaten en geen overgangsperiode: het zijn interne functies, er is geen externe aanroeper, en een alias die blijft staan is precies hoe deze naam zo lang kon blijven bestaan.

Verify: `grep -rn "nice_url" --include="*.py" opi/` geeft niets.

### 3. De tests meeverhuizen

`tests/test_nice_url_naming.py` en `tests/test_nice_url_auto_issuer.py` dragen de naam ook in hun bestandsnaam. Hernoem ze mee, zodat de bestandsnaam blijft zeggen wat hij toetst.

Verify: `grep -rn "nice_url" --include="*.py" tests/` geeft niets, en `ls tests/ | grep nice` is leeg.

### 4. De verwijzende documentatie bijwerken

`features/domain-configuration.md` en `operations-manager/python/features/domain-format.md` noemen het blok. Werk de naam bij, laat de uitleg over de verdwenen mode staan waar die historisch klopt.

Verify: geen `nice_url` meer in `features/`, wel nog de historische vermelding van `domain-mode: nice-url` waar die over de migratie gaat.

## Assertie

De wijziging is goed als:

1. `grep -rn "nice_url" --include="*.py" opi/ tests/` niets oplevert.
2. `uv run ruff check .` en `uv run pyright` schoon zijn.
3. De volledige testsuite hetzelfde resultaat geeft als vóór de wijziging. Meet dat vooraf en noteer het aantal; meet dat op main voordat je begint, en vergelijk erna. Op een naburige branch was dat op 27-08-2026 10028 geslaagd en 3 gefaald (`test_lotc_icon_mapping`, `test_template_structure`, `test_attachment_schema`); die drie falen los van deze wijziging, maar neem het getal van je eigen meting als ijkpunt.
4. `uv run python -c "from opi.core.cluster_config import get_domain_issuer; print(get_domain_issuer('fundament-poc','fundament-poc.rijksapp.dev'))"` geeft nog steeds `letsencrypt`. Dat is de kortste controle dat het blok na de hernoeming nog gelezen wordt.

## Wat er niet in zit

Het veld `supports_dots` behoudt zijn naam: die beschrijft precies wat hij doet. `domain-format` in projectbestanden verandert niet. Er komt geen migratie, want de naam staat in geen enkel projectbestand of schema.
