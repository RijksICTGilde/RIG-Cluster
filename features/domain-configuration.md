# Domain Configuration

> **Waar dit wordt opgeslagen (schemaversie 2.7):** de velden hieronder staan in het
> projectbestand onder `deployments[].services[publish-on-web].config`, niet meer los in de
> wortel van de deployment. Zie [webadres-onder-de-dienst.md](webadres-onder-de-dienst.md).
> De YAML-fragmenten in dit document tonen de velden zonder dat omhulsel, om over hun
> betekenis te gaan en niet over hun plek.

## Overview

Domain configuration controls how project components are accessible via URLs. Configuration is **per-deployment** - each deployment (e.g., production, staging, feature branches) independently defines its URL strategy.

## Domain Modes

Four domain modes determine how hostnames are constructed:

| Mode | Description | Example URL |
|------|-------------|-------------|
| `component-specific` | Each component gets a unique hostname based on its name | `frontend-myproject.cluster.example.com` |
| `deployment-name` | All components share the deployment name as subdomain | `production.cluster.example.com/frontend` |
| `custom` | User specifies a custom subdomain | `myapp.cluster.example.com` |
| `nice-url` | Dot-separated URLs with a registered domain | `frontend.myapp.rijksapp.nl` |

### Component-Specific Mode (Default)

Components each get a unique hostname using the pattern `{component}-{deployment}-{project}.{cluster_domain}`:

```yaml
deployments:
  - name: main
    cluster: local
    domain-mode: component-specific
    components:
      - reference: frontend
        image: nginx:latest
      - reference: api
        image: myapi:latest
# Results in:
#   frontend-main-myproject.kind
#   api-main-myproject.kind
```

### Deployment-Name Mode

All components share the same hostname, differentiated by path:

```yaml
deployments:
  - name: production
    cluster: local
    domain-mode: deployment-name
    components:
      - reference: frontend
        image: nginx:latest        # production-myproject.kind/
      - reference: api
        image: myapi:latest        # production-myproject.kind/api
```

### Custom Mode

User-specified subdomain replaces the default hostname prefix:

```yaml
deployments:
  - name: production
    cluster: local
    domain-mode: custom
    subdomain: myapp
    components:
      - reference: frontend
        image: nginx:latest        # myapp.kind/
```

### Nice-URL Mode

Dot-separated URLs using registered domains. Requires `subdomain` and `base-domain`:

```yaml
deployments:
  - name: production
    cluster: odcn-production
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    issuer: letsencrypt
    components:
      - reference: frontend
        image: nginx:latest
        root: true                 # myapp.rijksapp.nl
      - reference: api
        image: myapi:latest        # api.myapp.rijksapp.nl
```

## Configuration Fields

### `base-domain`

Specifies a custom base domain for the deployment. Available on **any deployment**, not just nice-url mode.

- In `nice-url` mode: replaces the cluster domain with a registered domain (e.g., `rijksapp.nl`)
- In other modes: overrides the cluster's `ingress_postfix` for hostname resolution

The domain must be listed in the cluster's `nice_url.supported_domains` configuration.

```yaml
deployments:
  - name: production
    base-domain: rijksapp.dev      # Use this instead of cluster default domain
```

### `domain-format`

Configurable hostname pattern that controls which variables appear in the generated hostname. When set, hostnames are generated from the selected template instead of the default logic for the domain mode.

Available formats:

| Format ID | Dash variant | Dot variant |
|---|---|---|
| `component-deployment-project` | `frontend-poc-myapp.kind` | `frontend.poc.myapp.rijksapp.dev` |
| `component-deployment-subdomain` | `frontend-poc-moza.kind` | `frontend.poc.moza.rijksapp.dev` |
| `deployment-project` | `poc-myapp.kind` | `poc.myapp.rijksapp.dev` |
| `deployment-subdomain` | `poc-moza.kind` | `poc.moza.rijksapp.dev` |

- **Dash variant**: used for clusters without nice-URL support
- **Dot variant**: used when the cluster supports nice URLs (dot-separated hostnames)

The field is optional and backward-compatible. See [domain-format.md](../operations-manager/python/features/domain-format.md) for full details.

```yaml
deployments:
  - name: production
    domain-format: deployment-subdomain
    subdomain: myapp
    base-domain: rijksapp.dev
```

### `subdomain`

Specifies a custom subdomain used in hostname generation. Its behavior varies by domain mode:

- **`nice-url`**: used as the subdomain segment (e.g., `frontend.{subdomain}.{base-domain}`)
- **`custom`**: used as the entire hostname prefix
- **`deployment-name`**: implicitly uses the deployment name
- **`component-specific`**: not typically used

```yaml
deployments:
  - name: production
    subdomain: myapp
```

### `issuer`

Controls TLS certificate provisioning. When set to a Let's Encrypt value, a namespace-scoped `Issuer` resource is automatically generated.

| Value | Description |
|-------|-------------|
| `letsencrypt` | Let's Encrypt production ACME server |
| `letsencrypt-staging` | Let's Encrypt staging ACME server (for testing) |
| _(custom)_ | References an existing namespace-scoped Issuer |

Automatically set to `letsencrypt` when using `nice-url` mode with a `base-domain` in the create wizard. See [external-domains-letsencrypt.md](external-domains-letsencrypt.md) for full TLS integration details.

```yaml
deployments:
  - name: production
    issuer: letsencrypt
    base-domain: rijksapp.nl
```

### `root-component`

> **TODO**: The `root: true` marker is a domain/routing concern living on a component reference alongside unrelated fields like `image` and `imagePullPolicy`. It only applies in one specific combination (nice-url mode + format with `{component}`). A cleaner approach would express this as an explicit ingress/path configuration rather than a boolean flag on the component reference. To be revisited when the domain model is refactored.

In `nice-url` mode with formats that include `{component}` in the hostname, each component gets its own subdomain (e.g., `frontend.myapp.rijksapp.nl`). The root component additionally receives an ingress on the bare subdomain (e.g., `myapp.rijksapp.nl`).

Specified as `root: true` on a deployment component reference:

```yaml
deployments:
  - name: production
    domain-mode: nice-url
    subdomain: myapp
    base-domain: rijksapp.nl
    components:
      - reference: frontend
        image: nginx:latest
        root: true                 # Also serves myapp.rijksapp.nl
      - reference: api
        image: myapi:latest        # Only api.myapp.rijksapp.nl
```

When `domain-format` is set to a template without `{component}` (e.g., `deployment-subdomain`), all components share the same hostname and root component marking is skipped.

### Component `path`

Each component has a publication path (default `/`) controlling URL routing when components share a hostname (e.g., `deployment-name`, `custom` modes).

Supports both simple string and multi-path list format:

```yaml
components:
  - name: frontend
    path: "/"                      # Simple string format

  - name: api
    path:                          # Multi-path list format
      - match: /api
        rewrite: /
      - match: /health
```

Each path generates its own Kubernetes Ingress resource. The `rewrite` field is optional and strips the matched prefix before forwarding to the service.

Paths can also be overridden per deployment - see [Deployment-Level Paths](#deployment-level-paths).

### Deployment-Level Paths

Paths can be specified on deployment component references to override the component-level `path`. When present, deployment-level paths take precedence.

```yaml
components:
  - name: frontend
    path: "/"                      # Default path

deployments:
  - name: production
    components:
      - reference: frontend
        image: nginx:latest
        paths:                     # Overrides component-level path
          - match: /app
            rewrite: /
```

Fallback chain:
1. `deployments[].components[].paths` (deployment-level override)
2. `components[].path` (component-level default)
3. `[{"match": "/", "rewrite": null}]` (system default)

## Cluster Base Domains

Each cluster defines which domains it supports for nice URLs in `CLUSTER_CONFIG`:

| Cluster | Supported Domains |
|---------|-------------------|
| `local` | `kind`, `local` |
| `sandboxed-local` | `sandbox.rijksapp.dev`, `rijksapp.nl`, `rijksapp.dev` |
| `odcn-production` | `rijks.app`, `rijksapps.nl`, `rijksapp.nl`, `rijksapp.dev` |

The cluster's `ingress_postfix` (e.g., `.kind`, `.rig.prd1.gn2.quattro.rijksapps.nl`) is used as the default domain when no `base-domain` is specified.

### Wat een client hiervan te zien krijgt

`GET /api/v2/projects/{p}/clusters` geeft per cluster `base-domains` (de lijst hierboven), `custom-domain-certificates` (wat een domein buiten die lijst hier oplevert) en `default-domain`.

`default-domain` is de `ingress_postfix` zonder de punt ervoor, en het staat er omdat de lijst zelf niet verraadt welke keuze meteen in gebruik gaat. De regel zit in `is_deployment_domain_approved`: een leeg `base-domain` en het domein van het cluster zelf gaan zonder goedkeuring, elke andere waarde vraagt er een aan en draait tot dat moment op het clusteradres. En dat clusterdomein kan gewoon als gewone entry in `base-domains` staan -- op `sandboxed-local` is `sandbox.rijksapp.dev` allebei -- dus "alleen de lege waarde is gratis" is aantoonbaar fout. Zonder dit veld moest een client het clusterdomein uit het label van de lege optie parsen.

Bewust het feit en niet het oordeel. Een veld dat per optie zegt of er goedkeuring nodig is, kan ook de stand van dit project meewegen (een domein dat dit project al goedgekeurd heeft vraagt niets meer), en dat is een keuze die nog niet gemaakt is; `default-domain` kan niet verkeerd zijn en sluit die rijkere variant niet uit.

## Complete YAML Reference

```yaml
name: my-project
display-name: My Application

components:
  - name: frontend
    path: "/"                          # Default path (simple string)
  - name: api
    path:                              # Multi-path (list format)
      - match: /api
        rewrite: /
      - match: /health

deployments:
  - name: production
    cluster: odcn-production
    namespace: my-namespace
    domain-mode: nice-url              # Required: one of 4 modes
    subdomain: myapp                   # Custom subdomain
    base-domain: rijksapp.nl           # Registered domain
    domain-format: deployment-subdomain # Optional: hostname template
    issuer: letsencrypt                # TLS certificate provisioning
    components:
      - reference: frontend
        image: nginx:latest
        root: true                     # Root component (nice-url only)
        paths:                         # Optional: override component paths
          - match: /app
            rewrite: /
      - reference: api
        image: myapi:latest
```

## Wizard Behavior

The create wizard produces a single "main" deployment. The domain step appears after the components step:

1. User selects a **domain mode** (defaults to `component-specific`)
2. For `nice-url` / `custom`: user provides a **subdomain**
3. For `nice-url`: user selects a **base domain** from cluster-supported options
4. For `nice-url`: user selects a **root component**
5. Optional: user selects a **domain format** template

On submit, the wizard:
- Assembles the deployment with `cluster`, `namespace`, `domain-mode`, `subdomain`, `base-domain`
- Maps `root-component` to `root: true` on the matching deployment component
- Auto-sets `issuer: letsencrypt` for `nice-url` with a base domain

## Domeinen en subdomeinen zijn op aanvraag

Een domein dat het cluster niet zelf aanbiedt, en een subdomein onder een domein met
`restricted-subdomains`, mogen pas gebruikt worden als een platformbeheerder ze heeft
goedgekeurd. De status staat op PROJECTniveau, onder
`services/[publish-on-web]/config/domains`, met per domein of subdomein een `status`
(`requested` / `approved` / `denied`) en de volledige verdicthistorie.

Die status is van het platform. Een API-client kan hem niet zetten en niet wissen: het veld
is als platform-eigendom gedeclareerd, de GET van het configblok laat het weg en een PUT
die het meestuurt krijgt 422. Zie `features/service-config-api.md`.

### De aanvraag ontstaat vanzelf

Een aanvraag is een gevolg van een deployment-schrijfactie, niet iets dat een client zelf
opschrijft. Zowel de portal als de API komen daarvoor uit bij dezelfde functie,
`connectors/subdomain.ensure_domain_requests`, zodat het om één soort aanvraag gaat, in
één blok, in één beheerdersinterface (`/admin/approvals`):

| Weg | Wat hem in gang zet |
|---|---|
| Portal (wizard en "Webadres bewerken") | Het verplichte vinkje "Domein aanvragen" / "Subdomein aanvragen", via `DomainRequestHook` |
| `PUT /api/v2/projects/{p}/services/publish-on-web/config/deployment/{d}` | De schrijfactie zelf, via `Service.ensure_approval_requests` |
| `POST` / `PUT /api/v2/projects/{p}/deployments[/{d}]` | De schrijfactie zelf, via `ensure_domain_requests` |

Een domein dat al is goedgekeurd levert geen nieuwe aanvraag op, en tweemaal schrijven
levert één aanvraag op: de functie leest de stand zoals die is en vult aan wat ontbreekt.

Wat de catalogus hiervoor kent, naast de bestaande `ApprovalSpec` (declareren, toetsen,
opsommen, oordeel vastleggen, melden), is `Service.ensure_approval_requests(project_data)`:
de vraag "wat vraagt dit project dat nog niemand heeft beoordeeld?". `publish-on-web` is
vandaag de enige dienst die hem beantwoordt; een dienst die niets declareert doet niets.

### Een niet-goedgekeurd domein blokkeert de deployment niet

De deployment rolt gewoon uit, maar op het standaard clusteradres
(`apply_domain_approval_fallback`). Dat is het lastige geval: er gaat niets stuk, er
verschijnt alleen geen ingress op het gevraagde adres. Daarom meldt de API de wachtstand
expliciet, in het veld `approvals`:

```json
{
  "approvals": [
    {
      "service": "publish-on-web",
      "type": "domain",
      "label": "Domein",
      "subject": "mijn-app.nl",
      "status": "requested",
      "text": "Het domein mijn-app.nl is aangevraagd en wacht op goedkeuring. Deze deployment is daarom bereikbaar op het standaard clusteradres.",
      "by": null,
      "date": "2026-08-15T09:14:31+00:00",
      "message": null
    }
  ]
}
```

De zin in `text` komt van de dienst zelf, want alleen die kent het gevolg. Het veld staat
op twee plaatsen, om dezelfde reden als `pending_rollout`:

- **in het antwoord op de schrijfactie** -- het taakresultaat van `configure_service` en
  van `upsert_deployment`, zodat een client meteen weet dat hij wacht;
- **op de leesendpoints** -- `GET .../deployments` en `GET .../deployments/{d}`, zodat hij
  het ook morgen nog kan opvragen. Een aanvraag loopt dagen; het antwoord op de PUT is weg
  zodra de client hem gelezen heeft.

De lijst is leeg wanneer alles wat de deployment vraagt is goedgekeurd. Een afgewezen
aanvraag komt terug met `status: "denied"` plus de `by`, `date` en `message` van het
oordeel, zodat "je wacht nog" en "je krijgt het niet" niet op hetzelfde uitkomen.

### De poort geldt voor elke vorm, niet alleen voor een domain-format

`apply_domain_approval_fallback` stond tot augustus 2026 BINNEN de tak van `get_component_ingress_map` die alleen gelopen wordt wanneer een deployment een `domain-format` noemt. Een deployment zonder dat veld -- een bestand van voor `domain-format` op `domain-mode: nice-url`, of een schrijfactie die alleen `base-domain` en `subdomain` zet -- viel in de oude dispatch daaronder en zette het gevraagde domein zonder enige controle in de hostnaam. Die deployments kregen dus een echte ingress op een domein dat niemand had goedgekeurd. De poort draait nu voor de vormkeuze; een goedgekeurd domein gaat ongewijzigd door.

Twee adressen die de poort ZELF samenstellen kwamen er langs, en zijn apart gesloten: de root-ingress van nice-url (`subdomain.base-domain`, inclusief certificaataanvraag) en dezelfde hostnaam in de lijst die naar de Keycloak-redirects gaat.

### Het getoonde adres is het bediende adres

`get_component_ingress_map` levert zowel de hostnaam van de ingress als het adres dat de portal (`publish_on_web/urls.py`) en de API (`urls` op de deployment-endpoints) tonen. Er is dus EEN antwoord op "waar draait dit"; een tweede afleiding ernaast is precies hoe de portal een adres kon tonen dat niets bediende.

In de portal staat de melding uit `approvals` naast de publieke links, op het tabblad Componenten en op Deployments (`approval_alerts` in `bg/_patterns.html.j2`). De tekst komt van de dienst, dus UI en API zeggen hetzelfde, met hetzelfde onderscheid tussen `none`, `requested` en `denied`.

### Nog open: adressen die buiten deze afleiding om worden samengesteld

Deze drie stellen een deploymentbrede hostnaam zelf samen uit `subdomain` + `base-domain`, zonder de goedkeuringspoort en zonder `domain-format`:

- `ProjectManager._get_deployment_alias_context` (`project_manager.py`) vult `PUBLIC_HOST`, `PUBLIC_HOSTNAME`, `HOSTNAME` en `BASE_DOMAIN` voor de aliassen, dus een applicatie krijgt het niet-goedgekeurde adres als omgevingsvariabele terwijl haar ingress op het clusteradres staat;
- `BootstrapManager` bouwt `public_host` voor de bootstrapacties op dezelfde manier;
- `KeycloakManager` voegt voor helm- en helmfile-deployments een `subdomain.base-domain`-hostnaam aan de redirect-URI's toe.
