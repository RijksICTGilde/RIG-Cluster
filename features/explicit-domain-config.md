# Feature: Expliciete Domein Configuratie met Format Strings

## Probleem

De huidige domein configuratie in project YAML's is impliciet: 4 verschillende modi worden bepaald door welke keys aanwezig zijn (`domain-mode`, `subdomain`, `base-domain`). Dit maakt het verwarrend voor gebruikers en fragiel in code (veel if/else branching).

### Huidige situatie

| Keys aanwezig | Gedrag | Voorbeeld resultaat |
|---|---|---|
| Geen | Cluster default | `component-deployment-project.rig.prd1.gn2.quattro.rijksapps.nl` |
| `subdomain` == deployment name | Deployment-name mode | `deployment-1-project.kind` |
| `subdomain` + `base-domain` | Custom domain (DASHES) | `amt.rijksapp.nl` |
| `domain-mode: nice-url` + `subdomain` + `base-domain` | Nice URL (DOTS) | `component.bouwmeester.rijks.app` |

## Oplossing

Een expliciet `domain` blok op deployment-niveau met een format-string die exact beschrijft hoe hostnames worden opgebouwd. Paths verhuizen van component-definities naar deployment component-references.

### Nieuw YAML formaat

```yaml
deployments:
- name: productie
  cluster: odcn-production
  namespace: amt-odc-prd
  domain:
    root: rijksapp.nl                # Het geregistreerde domein
    subdomain: amt                   # Alles voor het root domein
    format: "{subdomain}.{root}"     # Expliciet hostname formaat
    issuer: letsencrypt              # TLS issuer
  components:
  - reference: component-1
    image: ghcr.io/example:latest
    paths:                           # Paths nu op deployment component ref
    - match: /
  - reference: component-2
    image: ghcr.io/example2:latest
    paths:
    - match: /api
      rewrite: /
```

Geen `domain` blok = cluster default: `{component}-{deployment}-{project}.{cluster_domain}`

### Format variabelen

| Variabele | Beschrijving | Voorbeeld |
|-----------|-------------|-----------|
| `{component}` | Component naam | `component-1` |
| `{deployment}` | Deployment naam | `productie` |
| `{project}` | Project naam | `amt-odc-prd` |
| `{subdomain}` | Subdomain uit domain config | `amt` |
| `{root}` | Root domein uit domain config | `rijksapp.nl` |

### Presets (UI suggesties, in YAML gewoon de format string)

| Naam | Format | Voorbeeld resultaat |
|------|--------|-----------|
| Gedeeld domein | `{subdomain}.{root}` | `amt.rijksapp.nl` |
| Per-component | `{component}.{subdomain}.{root}` | `frontend.amt.rijksapp.nl` |
| Cluster default | _(geen domain blok)_ | `comp-deploy-proj.cluster.domain` |

### Validatie

- Als de format-string geen `{component}` bevat, delen alle components in het deployment hetzelfde hostname. In dat geval moeten paths uniek zijn per component.
- Format-strings worden gevalideerd: alleen bekende variabelen, syntactisch correct, resulterend in geldige DNS-namen.

---

## Implementatieplan

### Fase 1: DomainConfig datamodel + hostname generatie

**Doel**: Nieuwe pure functies naast de bestaande, zero I/O.

**Nieuwe code in `operations-manager/python/opi/utils/naming.py`**:

1. `DomainConfig` frozen dataclass met velden: `root`, `subdomain`, `format`, `issuer`
2. `resolve_hostname_from_format()` - genereert hostname uit format string + variabelen
3. `extract_domain_config(deployment: dict) -> DomainConfig | None` - leest het nieuwe domain blok
4. `validate_format_string()` - valideert format string syntax en variabelen
5. `format_contains_component()` - check of format `{component}` bevat
6. `get_component_ingress_map_v2()` - nieuwe versie die `DomainConfig | None` accepteert

**Tests**: Unit tests die aantonen dat elke oude modus reproduceerbaar is via format string.

---

### Fase 2: Path extractie uit deployment component references

**Doel**: `ProjectFileHandler` kan paths lezen van de nieuwe locatie met fallback.

**Nieuwe code in `operations-manager/python/opi/handlers/project_file_handler.py`**:

1. `extract_deployment_component_paths()` met fallback keten:
   - Eerst: `deployments[].components[ref].paths` (nieuw)
   - Fallback: `components[name].path` (oud)
   - Default: `[{"match": "/", "rewrite": null}]`

---

### Fase 3: Path uniciteits-validatie

**Doel**: Valideer dat paths uniek zijn wanneer components een hostname delen.

**Nieuwe code in `operations-manager/python/opi/utils/project_utils.py`**:

1. `validate_deployment_paths()` - controleert uniciteit wanneer format geen `{component}` bevat

---

### Fase 4: Project Manager (primaire consumer)

**Doel**: Alle domain/path reads in `project_manager.py` overzetten naar de nieuwe structuur.

**Aanpassingen in `operations-manager/python/opi/manager/project_manager.py`**:

1. Domain config extractie: `domain-mode`/`subdomain`/`base-domain` -> `extract_domain_config(deployment)`
2. Hostname generatie: `HostnameFormat.from_domain_mode()` -> `get_component_ingress_map_v2()`
3. Path extractie: `extract_component_paths()` -> `extract_deployment_component_paths()`
4. Subdomain registratie: check op `domain_config.subdomain` i.p.v. `domain_mode == "nice-url"`
5. Clone exclusion: losse keys -> `["domain"]`
6. Environment variabelen: `HOSTNAME`/`PUBLIC_HOST` via `resolve_hostname_from_format()`
7. Root ingress: voor formats met `{component}` (component-specifieke hostnames + root hostname)

---

### Fase 5: Keycloak Manager

**Doel**: SSO hostname generatie overzetten.

**Aanpassingen in `operations-manager/python/opi/manager/keycloak_manager.py`**:

1. Hostname collectie via `extract_domain_config()` + `get_deployment_hostnames()` met `DomainConfig`

---

### Fase 6: Web Router (display)

**Doel**: Project details en POST handler overzetten.

**Aanpassingen in `operations-manager/python/opi/web/router.py`**:

1. Display: hostname tonen via `DomainConfig`
2. POST handler: form data mappen naar `domain` blok

---

### Fase 7: Forms/Wizard

**Doel**: Formulier velden en wizard assemblage overzetten.

**Aanpassingen in meerdere files**:

1. `forms/editables/fields/domains.py`: yaml_paths naar `deployments[0]/domain/root`, etc.
2. `forms/providers.py`: `DomainModeOptionsProvider` vervangen door format presets
3. `web/router_wizard.py`: `_assemble_deployment()` bouwt `domain` blok
4. `utils/project_utils.py`: `generate_self_service_project_yaml()` bouwt `domain` blok
5. `forms/editables/wizard_sections.py`: sectie layout aanpassen

---

### Fase 8: Cluster Config

**Doel**: Herstructureer cluster domein config.

**Aanpassingen in `operations-manager/python/opi/core/cluster_config.py`**:

1. Voeg `domain.cluster_domain` toe (was `ingress_postfix` zonder punt)
2. Voeg `domain.supported_root_domains` toe (was `nice_url.supported_domains`)
3. Houd `ingress_postfix` als deprecated alias

---

## Migratie

Migratie van bestaande project YAML bestanden is een apart traject. De nieuwe code ondersteunt zowel het oude als het nieuwe formaat via fallback-logica in `extract_domain_config()` en `extract_deployment_component_paths()`.

## Verificatie

Per fase:
- Unit tests voor nieuwe functies
- `uv run ruff check . --fix && uv run ruff format .`
- `uv run pyright`
- End-to-end: test met project YAML in nieuw formaat door volledige manifest generatie flow
