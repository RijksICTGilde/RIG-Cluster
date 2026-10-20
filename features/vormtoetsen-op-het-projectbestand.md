# Vormtoetsen op het projectbestand

Wat het is: de velden van een projectbestand waarvan de VORM op elke schrijfweg wordt
getoetst, met per veld een bron voor die vorm. Niet de grenzen (hoeveel geheugen mag ik
vragen) en niet de rechten (mag ik dit zetten), alleen de vorm: welke tekens, welk patroon.

## Waarom

Een projectbestand komt langs vier wegen binnen: het formulier, de REST-API, een
service-config PUT en git (iemand bewerkt het bestand met de hand). Het formulier had op
een aantal velden al een validator, en de andere drie wegen zagen die regel niet. Zo kwam
een hoeveelheid met een spatie, een e-mailadres met whitespace of een pad met een quote
door het schema, om pas bij het renderen van een manifest problemen te geven.

De validatie van een projectbestand heeft één chokepoint, `GitProjectStore._validate`, en
die draait het JSON-schema plus de structurele toetsen (waaronder de typed configmodellen
van de diensten). Een vorm die daar staat, geldt dus voor alle vier de wegen.

## Eén bron per patroon

JSON kan niet importeren, dus een patroon dat zowel in het schema als in Python staat, is
twee teksten. `tests/test_project_schema_validation.py` toetst dat ze gelijk zijn.

| veld | vorm | bron |
|---|---|---|
| `components[*]/resources/**`, `deployments[*]/components[*]/resources/**` | Kubernetes-hoeveelheid (`500m`, `1`, `256Mi`, `1.5Gi`) | `K8S_QUANTITY_PATTERN`, `opi/services/resource_analyzer.py`, overgenomen in `$defs/k8s-quantity` |
| `env-vars`, `user-env-vars` als mapping | variabelenaam als sleutel, waarde zonder CR, LF of NUL | `ENV_VAR_NAME_PATTERN`, `opi/utils/env_vars.py`, overgenomen in `$defs/env-var-map` |
| `users[*]/email` | e-mailadres | `EMAIL_PATTERN`, `opi/forms/editables/validators.py`, overgenomen in `$defs/user` |
| `components[*]/name`, `deployments[*]/name`, `deployments[*]/components[*]/reference` | DNS-1123-label dat met een letter begint | `$defs/dns-1123-label-letter-first` |
| `deployments[*]/cluster`, `deployments[*]/namespace` | DNS-1123-label | `$defs/dns-1123-label` |
| `components[*]/image`, de `image` van een eigen PostgreSQL-cluster | image-verwijzing | `CONTAINER_IMAGE_PATTERN`, `opi/utils/naming.py` |
| component-`path` (`match`, `rewrite`), `logout-path`, het pad van de metrics-scraper | URL-pad van een tenant | `SAFE_PATH_PATTERN`, `opi/utils/url_paths.py` |
| `subdomain` van publish-on-web | DNS-1123-label, kleine letters | `SUBDOMAIN_PATTERN`, `opi/connectors/subdomain.py` |
| `base-domain` van publish-on-web | domeinnaam | `DOMAIN_PATTERN`, `opi/connectors/subdomain.py` |

Een patroon eindigt op `\Z` en niet op `$`: in Python matcht `$` ook vóór een afsluitende
newline, dus `/metrics\n` en `mijn-app\n` kwamen er anders door.

## De backstop

`find_forbidden_characters` (`opi/utils/forbidden_characters.py`) weigert in ELKE
stringwaarde en elke sleutel van het bestand:

- NUL en de overige Cc-tekens, behalve tab en newline (een commandoveld bevat
  heredoc-scripts, dus die twee blijven);
- de bidi-controls, want tekst die anders leest dan ze is misleidt een reviewer
  (Trojan Source).

Hij verandert niets: geen trim, geen normalisatie, geen lengte. Een sanitizer die NA de
validatie draait wijzigt een goedgekeurde waarde (CWE-180). De melding noemt het pad en de
naam van het teken, nooit de waarde: die kan een secret zijn en de melding wordt gelogd.

Hij vangt de velden zonder eigen patroon, en daarmee de schrijfwegen die geen editable
kennen: een approval met een ruwe JSON-body, een service-config PUT en programmatisch
schrijven.

## Wat dit niet is

**Geen outputencoding.** De tweede laag zit aan de sink: `| tojson` in de
manifestsjablonen, autoescape in Jinja, argv-lijsten naar subprocessen. Validatie aan de
bron vervangt die niet.

**Geen grens.** Hoe groot een volume of een geheugenlimiet mag zijn, is een declaratie van
de dienst: `speelruimte-van-een-dienst.md`.

**Geen sanitizing.** Trimmen en normaliseren van invoer hoort vóór de validatie, in de
editable: plan `invoer-eerst-schoon-dan-getoetst.md`.

## Afhankelijkheden

- `opi/services/project_store.py` -- `_validate`, het chokepoint
- `opi/schemas/project_v2.json` -- de `$defs` met de patronen
- `opi/core/project_schema.py` -- de validator, met `format_checker` aan (`format` doet pas
  iets mét een checker; het werk doet het `pattern` ernaast)
- `opi/utils/forbidden_characters.py` -- de backstop
- `tests/test_project_schema_validation.py` -- de gelijkheid van schema en constante
