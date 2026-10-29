# De kaart van ZAD: een drop-in element, een projecttab en een platformpagina

Status: **contract, 29 oktober 2026.** De besluiten in paragraaf 3 zijn genomen. Gemeten op `forgejo/main` `2287fc69b`. Paden zijn relatief aan `operations-manager/python/` tenzij ze anders beginnen.

## 0. Hoe je dit leest

Er bestaat een werkende spike. Die is het vertrekpunt: neem de code over, bouw hem niet opnieuw.

- Spike: https://git.claude.robbertuittenbroek.nl/robbert/zad-map, commit `c17753e`. Svelte 5, `@xyflow/svelte`, `elkjs`, `roughjs`, `@nldd/design-system`. De README daar zegt hoe je hem draait.
- In de spike leest de app JSON uit eigen extract-scripts en kent hij ZAD-begrippen (component, service, deployment). In dit plan wordt dat gesplitst: een generiek element in de browser, en de ZAD-kennis in Python.

Namen in paragraaf 3 zijn besloten. Namen van code-objecten met VOORSTEL ervoor mag je anders kiezen als je in de PR zegt waarom.

## 1. Doel

Een team ziet zijn project als kaart: welke componenten er zijn, welke services ze gebruiken, welke deployments er draaien en welke koppelingen er met andere projecten lopen. Wie toegang heeft tot ZAD ziet daarnaast hoe het platform zelf in elkaar zit.

**Klaar is:**

1. Er is een custom element `<zad-map src="...">` in één bestand, `static/js/zad-map.js`, dat een graaf volgens contract v1 (paragraaf 4) tekent, met shadow DOM, zonder ZAD-kennis.
2. De projectpagina heeft een tab **Kaart** op `/projects/{project_name}/kaart`, met de data van `/projects/{project_name}/kaart.json`, achter dezelfde poort als de andere projecttabs.
3. Er is een pagina **Platform** op `/platform`, in het hoofdmenu, voor iedereen die ingelogd is en toegang heeft tot ZAD.
4. Er komt geen nieuwe schrijfweg bij (paragraaf 7).
5. Weghalen is vier dingen verwijderen: de tabregel, de routes, de bouwer in Python en het bestand in `static/js`. Niets anders in OPI hangt ervan af.

## 2. De uitgangssituatie

**Projecttabs.**
- `PROJECT_TABS` staat in `opi/web/lotc_switch.py:614-630`, als `{key: {"label", "path"}}`, in de volgorde van de tabbalk. Daarnaast: `project_tab_url` (657), `tab_from_path` (704) en `build_lotc_project_details` (716).
- De routes zijn een stapel decorators in `opi/web/router.py:1347-1360`, onder `@action(Action.PROJECT_READ)` en `@requires_sso`. Ze roepen allemaal `render_project_page` aan (1457).
- De paden staan er letterlijk, niet als `{tab}`. Zie de docstring op 1462.
- De template is `opi/templates_lotc/bg/project-tabs.html.j2`, met een `{% elif active_tab == '...' %}` per tab. Scripts horen in `{% block head_extra %}`, als `<script nonce="{{ csp_nonce }}" src="{{ static_url('js/x.js') }}">`.
- `render_project_page` doet per tab zwaar werk: secrets ontsleutelen, ArgoCD bevragen. Team, backups en audit slaan dat over met `tab_from_path(request.url.path) == ...` (1844, 1868, 1909).
- Data die je laat ophalen gaat zoals bij metrics: een eigen route met een eigen action (`router.py:2529-2534`).

**Rechten.**
- Elke route in `opi/web` gaat door `enforce_route_action` (`opi/web/authz.py:193`). Die leest `@action(...)`, en bij een project-action met `project_name` in het pad roept hij `authorize` aan: 404 of 403.
- "Ingelogd met toegang tot ZAD" is `@action(Action.PORTAL_READ)` + `@requires_sso`, zoals op `/projects` (`router.py:2712`). De echte poort is dan de middleware: geen gebruiker betekent login, geen toegang betekent `/permission-denied` (`opi/middleware/authorization.py:112-129`, `user_service.is_email_allowed`).
- Guards die elke route raken: `tests/test_elke_route_heeft_een_herkenning.py` (SSO-markering), `tests/test_actions.py` (een action op elke route) en `tests/oppervlak_snapshot.json`.

**Data.**
- `get_project_store()` (`opi/services/project_store.py:1359`) geeft `.get(name)` en `.get_all()`. Die leveren `ProjectSummary`, met `data` als de ruwe YAML.
- `.get_decrypted(name)` bestaat ook, maar is **niet** nodig voor de kaart (paragraaf 7).
- Webadressen: `public_urls_for_deployment(project_data, deployment, project_name, project_file_handler)` in `opi/services/catalog/publish_on_web/urls.py:26`. Die geeft per component `component_name`, `hostname`, `url`, en wordt ook door de projectpagina gebruikt (`router.py:1754`).
- Resources: `ProjectFileHandler.extract_component_resources` (`opi/handlers/project_file_handler.py:1471`) met daaroverheen `extract_deployment_component_resources` (1490), zoals in `project_manager.py:6331-6339`.
- Image per deployment: `deployments[].components[].image`. Het `image` in de componentdefinitie wordt bij het uitrollen niet gebruikt (`project_manager.py:6251`), dus toon het niet als waarheid.
- Servicetitels: `get_service_by_value(...)` en `ServiceAdapter.get_service_definition(...)` (`opi/services/services.py:545-555`). Uitleg: `service_help_markdown` en `render_service_help` in `opi/services/help_text.py` (87, 241).
- Cross-domain: `merge_rules` en `to_merged_rule` in `opi/services/catalog/cross_domain_access/merge.py:74,109`, met `MergedRule(name, peer_project, peer_deployment, peer_component, local_component, port, is_open)`.
  - **Er is geen omgekeerde zoekfunctie.** Niets geeft "welke projecten wijzen naar dit project".
  - De inbound-regel van de ontvanger is de toestemming; outbound is de bedoeling. `from.project == "*"` is een open regel.

**Statische JS.**
- `static/js/codemirror-bundle.js` is met de hand gebouwd en ingecheckt. Het script staat in `static/js/package.json`; er is geen CI-stap of Docker-stap voor.
- `node_modules` staat in `.gitignore` en `.dockerignore`. Overgenomen bibliotheken staan in `static/HERKOMST.md`.

**CSP.**
- `opi/middleware/security_headers.py`: `script-src 'self' 'nonce-…' 'strict-dynamic'`, geen `unsafe-eval`, `style-src 'self' 'unsafe-inline'`, `connect-src 'self'`.
- Gemeten op de spike-build: `elk.bundled.js` en de bundel bevatten geen `eval(` en geen `new Function(`.
- Een web worker voor ELK gaat onder `strict-dynamic` niet, want daar is geen `worker-src`. ELK draait dus op de hoofdthread.

**Image.**
- `operations-manager/Dockerfile:161-180` kopieert alleen `opi/`, `manifests` en `static/{media,css,js,...}`.
- `infrastructure/` en `features/` zitten **niet** in de image. Wat de platformpagina tijdens het draaien nodig heeft, moet onder `opi/` staan.

**Node** is er in de orch-sandbox, via mise `node@lts`.

## 3. Besluiten

| # | besluit |
|---|---|
| 1 | De kaart is een custom element `<zad-map>` met shadow DOM, gevoed door een generieke graaf (contract v1). Het element weet niets van ZAD. |
| 2 | De bron van het element staat in `operations-manager/zad-map/` (eigen `package.json`). De build schrijft `operations-manager/python/static/js/zad-map.js`, met de hand gebouwd en ingecheckt zoals `codemirror-bundle.js`. |
| 3 | De ZAD-kennis (welke knopen, welke lijnen, welke uitleg) zit in Python, in één pakket, en gebruikt de bestaande functies. Er wordt niets nagebouwd. |
| 4 | De projectkaart is een tab op de projectpagina, achter `PROJECT_READ`. Er is geen projectkiezer: je ziet het project van de pagina. |
| 5 | De kaart toont cross-domain koppelingen **in beide richtingen**: inkomend en uitgaand, met de naam van het andere project en component. |
| 6 | De platformpagina is `/platform`, voor wie ingelogd is en toegang heeft tot ZAD (`PORTAL_READ`), en staat in het hoofdmenu. Niet publiek. |
| 7 | De platformpagina toont alleen het cluster waar deze OPI draait (`CLUSTER_MANAGER`). Er is geen schakelaar. |
| 8 | De platformdata wordt in de repo gegenereerd en als JSON onder `opi/` ingecheckt. Een test faalt als het bestand niet meer overeenkomt met de repo. |
| 9 | Vormgeving zoals de spike: schetsvormen (Rough.js, vaste seed per knoop), strakke orthogonale lijnen van ELK, NLDD-tokens voor oppervlak, tekst en accent, en Rijkshuisstijl-kleuren per categorie. |
| 10 | Niet in deze taak: het landschap van alle projecten (admin), live status uit ArgoCD en een donker thema. |

## 4. Het contract: graaf v1

Het element haalt `src` op (GET, same-origin, met cookies) en tekent wat er staat. Alle tekst is platte tekst, behalve `html` in `info`.

```jsonc
{
  "version": 1,
  "columns": [{ "id": "c0" }, { "id": "c1" }, { "id": "c2" }],   // links naar rechts
  "groups": [
    { "id": "deps", "title": "Deployments", "column": "c2", "pack": "grid" },
    { "id": "ext:mwt-ked", "title": "mwt-ked", "eyebrow": "ander project", "column": "c3",
      "pack": "elk", "style": "external" },
    { "id": "base", "title": "Geleverd door ODCN", "subtitle": "...", "band": true, "pack": "row" }
  ],
  "nodes": [
    { "id": "c:editor", "column": "c1", "shape": "card", "color": "component",
      "eyebrow": "deployment", "title": "editor",
      "rows": [ { "bar": "mem", "req": 121, "lim": 1442, "max": 1640, "text": "1442Mi" },
                { "icon": "health", "text": "/healthz" } ],
      "click": "focus", "info": { ... } },
    { "id": "s:keycloak", "column": "c2", "shape": "pill", "color": "identity",
      "title": "Keycloak Authentication", "subtitle": "keycloak", "click": "focus", "info": { ... } },
    { "id": "d:upload", "group": "deps", "shape": "card", "color": "deployment",
      "title": "upload", "subtitle": "odcn-production",
      "click": "expand", "related": ["c:upload", "c:editor"],
      "children": [ { "id": "di:upload:editor", "shape": "card", "title": "editor",
                      "rows": [ { "icon": "box", "text": "regelrecht-editor:latest" },
                                { "icon": "globe", "text": "editor-upload-...", "href": "https://..." } ] } ] },
    { "id": "team", "column": "c0", "shape": "stack", "color": "team", "title": "5 leden",
      "eyebrow": "Team", "click": "expand", "children": [ ... ] }
  ],
  "edges": [ { "from": "c:editor", "to": "s:keycloak", "color": "identity" },
             { "from": "c:democonsole", "to": "xc:mpfb-8wh:redis", "color": "link", "label": ":6379" } ]
}
```

Regels voor het element:

- **Layout.**
  - ELK `layered`, richting rechts, `INCLUDE_CHILDREN`, met partities uit `columns`.
  - Per lijn een eigen port aan beide kanten, `FIXED_SIDE`.
  - Orthogonale routing, met minstens 14px tussen banen en tussen ports.
  - Een knoop is minstens zo hoog als zijn aantal lijnen keer 14 plus marge.
  - Een groep met `pack: "grid"` legt het element zelf in een raster, en is voor ELK één blok. Gebruik dit alleen voor groepen waar geen lijnen naar binnen lopen; ELK zet anders alles in één kolom.
  - Een `band` komt onder alles, zo breed als de plaat.
  - Neem deze opties over uit de spike (`src/lib/graph.js`, `layout`, `pack`, `projectGraph`).
- **Vormen:** `card`, `pill`, `stack`, `hub`, `frame` en `external` (stippelrand). `state` met de waarde `elders`, `extern` of `uit` geeft een stippelrand en een eyebrow.
- **Kleuren** zijn namen, geen hex: `component`, `deployment`, `team`, `project`, `web`, `identity`, `data`, `ops`, `link`, `gitops`, `network`, `platform`, `external`. Het element vertaalt ze naar de Rijkshuisstijl-kleuren uit de spike.
- **Klikken.**
  - `focus`: dim alles behalve de knoop, zijn voorouders, zijn inhoud, alles wat er via een lijn aan vastzit, en de knopen in `related`.
  - `expand`: klap open en dicht, met animatie, zoom naar de inhoud en geef focus.
  - `info`: open meteen het paneel.
  - Een knoop met `info` krijgt rechtsboven een "i"-knop die het paneel opent. In een pil zit die rechts, verticaal gecentreerd, met even veel marge links als rechts, zodat de tekst in het midden blijft.
  - Een klik op het lege canvas laat alles los. Het kruisje van het paneel sluit alleen het paneel.
- **Paneel.**
  - `info` is `{ "eyebrow", "title", "subtitle", "sections": [ { "heading", "items": [ { "text", "mono", "href" } ] } | { "heading", "html" } ] }`.
  - Het element vuurt `zad-map-info` met `detail: { id }`. Met het attribuut `no-panel` laat het element het paneel aan de pagina over.
- **Bediening:** zoomknoppen, een minimap en een knop voor volledig scherm (Fullscreen API op het element).
- **Vormgeving:** NLDD-tokens via CSS custom properties, die door de shadow DOM heen erven: `--semantics-surfaces-*`, `--semantics-content-*`, `--semantics-dividers-color`, `--primitives-font-family-*`. Zonder NLDD gelden nette standaardwaarden. Neem `app.css`, `sketch.js`, `SketchNode.svelte` en `SketchEdge.svelte` uit de spike over.
- **Grootte:** het element vult zijn container. De pagina geeft het een hoogte.

## 5. Wat er moet gebeuren

### 1. Het element

Neem de spike over naar `operations-manager/zad-map/` en maak er een custom element van (Svelte `customElement`, shadow DOM). Haal eruit wat bij de spike-app hoorde: de projectkiezer, de kruimels, het landschap, de routering en de extract-scripts. Laat `graph.js` het contract van paragraaf 4 lezen in plaats van projectdata. Build: één IIFE- of ES-bestand, `static/js/zad-map.js`, met de CSS erin.

Daarnaast:

- een taak in `Taskfile.yaml` (VOORSTEL `zad-map:build`);
- een regel in `static/HERKOMST.md` met de versies;
- een voorbeeldpagina `operations-manager/zad-map/example.html` met een voorbeeld-graaf, om het element los te draaien.

*Bewijs:*

- een test die `static/js/zad-map.js` leest en faalt op `eval(` of `new Function(`;
- de Playwright-test van stap 5, die de tab laadt onder de echte CSP-header van de app.

### 2. De graafbouwer in Python

Eén pakket (VOORSTEL `opi/services/kaart/`):

- **Contract v1** als Pydantic-modellen, zodat elke uitvoer gevalideerd is.
- **`project_graph(project_name) -> Graph`:**
  - Kolommen:
    - kolom 0: de projectkaart (naam, code; de omschrijving in `info`), het team (inklapbaar naar de leden met hun rol) en de projecten die naar dit project wijzen;
    - kolom 1 en volgende: de componenten (bij meer dan zes verdeeld over kolommen, zoals in de spike);
    - daarna: de services;
    - daarna: het kader Deployments;
    - helemaal rechts: de projecten waar dit project naar wijst.
  - Lijnen:
    - component naar service, in de categoriekleur;
    - cross-domain, in `link`, met de poorten als label (één label per doel);
    - `uses-components`, als het gevuld is.
  - Een component toont als rijen:
    - geheugen en CPU als staafjes: request en limit, met als schaal het hoogste limit in het project;
    - het health-pad (`health-check` liveness-path);
    - de schijven (`persistent-storage`, `temp-storage`: maat en mount);
    - het metrics-pad;
    - het aantal bijlagen.

    Géén poorten, géén env-vars, géén "achter keycloak". Die keuzes zijn met de eigenaar gemaakt.
  - Een deployment klapt open naar zijn componenten, met per component de image-tag en het webadres uit `public_urls_for_deployment`. Een deployment bevat niet altijd alle componenten. `related` wijst naar de componentdefinities.
  - Een service heeft als `info` de titel en de gerenderde `help.md` (`render_service_help`), en "gebruikt door" met de componenten.
- **`incoming_links(project_name)`:** de omgekeerde zoekfunctie. Loop over `get_project_store().get_all()`, lees per project de cross-domain-regels (project- en deploymentlaag, via `merge_rules` en `to_merged_rule`), en geef de regels waarvan `peer_project` dit project is. Ontdubbel op (van project, van component, naar project, naar component, poort). Een peer `*` wordt de knoop "Alle projecten".

*Bewijs:* unittests op `project_graph` met fixtures:

- een project met een uitgaande en een inkomende koppeling (een ander project dat naar dit wijst), waarbij beide in de graaf staan, met de juiste projectnamen en één label per doel;
- een deployment met een deel van de componenten, waarbij alleen die componenten als kinderen staan, met het webadres uit `public_urls_for_deployment`;
- een component zonder `resources`, dat geen staafjes krijgt en geen fout geeft;
- de uitvoer valideert tegen het contract.

### 3. De projecttab

- **Tab:** `"kaart": {"label": "Kaart", "path": "kaart"}` in `PROJECT_TABS`, en de route `/projects/{project_name}/kaart` in de stapel van `router.py:1347`.
- **Zware stappen overslaan:** in `render_project_page` het zware werk overslaan bij `tab_from_path(...) == "kaart"`, op de manier van team, backups en audit.
- **Template:** in `project-tabs.html.j2` een `{% elif active_tab == 'kaart' %}` met `<zad-map src="/projects/{{ project.name }}/kaart.json">` in een container die tot onder de tabbalk loopt (VOORSTEL: `height: calc(100vh - <kop>); min-height: 520px`), en het script met de nonce in `head_extra`, alleen op deze tab.
- **Data:** de route `/projects/{project_name}/kaart.json` met `@action(Action.PROJECT_READ)` en `@requires_sso`. Die geeft `project_graph(...)` als JSON terug.

*Bewijs:*

- de bestaande guard-tests slagen (SSO-markering, action), en `oppervlak_snapshot.json` is bijgewerkt;
- een test in de vorm van `tests/test_audit_tabblad.py`: een gebruiker die geen lid is, krijgt 404 of 403 op `kaart.json` en op de tab;
- `tests/test_lotc_tabbladen_url.py` kent de nieuwe tab.

### 4. De platformpagina

- **Generator** (VOORSTEL `opi/tools/kaart_platform.py`): neemt de werking en de tabellen van `scripts/extract_platform.py` uit de spike over. Hij leest `infrastructure/bootstrap/clusters/*/kustomization.yaml`, `opi/connectors`, `opi/services/catalog/*/help.md` en `features/*.md`, en schrijft `opi/services/kaart/platform.json`.
  - **Status per cluster:**
    - `aan` of `uit` volgens de cluster-kustomization (een regel met `#` telt als uit);
    - `elders` als er wel een overlay voor dat cluster is, maar geen regel;
    - `afwezig` als het systeem in dat cluster niet bestaat;
    - `cluster` als wij er helemaal geen overlay voor hebben;
    - `extern` als er geen map in `infrastructure/` is.
  - **Handtabellen:** de drie met de hand ingevulde tabellen (connector naar systeem, catalogus-service naar systeem, laag per systeem) blijven tabellen in de generator, met uitleg waarom ze niet uit de code volgen.
- **Route:** `/platform` met `@action(Action.PORTAL_READ)` en `@requires_sso`, plus een template met `<zad-map src="/platform.json">`. De bijbehorende `/platform.json` krijgt dezelfde markeringen.
  - **Cluster kiezen:** de route kiest uit het JSON-bestand het cluster van `CLUSTER_MANAGER`, en vertaalt dat naar de mapnaam: `odcn-production` wordt `odcn`, `odcn-staging` wordt `odcn-staging`, `sandboxed-local` en `local` worden `sandboxed-local`.
  - **Zichtbaar is:** `aan`, `elders` en `extern`, systemen met een OPI-connector, en in `odcn` ook `cluster`.
- **Plaat:**
  - links het kader ZAD, met de Operations Manager als hub en Secrets (SOPS);
  - rechts de kaders GitOps (Git-repo's → ArgoCD), Services voor projecten en Netwerk en DNS, elk met één lijn vanaf de hub en zonder label;
  - onderaan de band "Geleverd door ODCN", in de sandbox "Cluster-fundament". In de band is `extern` de norm, dus daar komt geen stippelrand.
- **Info per systeem:** wat het is, de status, de connector met het aantal imports, de services die erop leunen, en "Meer lezen" met de titels en paden van de feature-documenten. Dat zijn alleen tekst en geen links, want `features/` zit niet in de image.
- **Menu:** een regel in `opi/web/menu.py`, `{"label": "Platform", "link": "/platform", "icon": ...}`, met een icoon uit de bestaande set.

*Bewijs:*

- een test die de generator draait en het resultaat vergelijkt met de ingecheckte `platform.json`. Die faalt met de melding welk commando het bestand bijwerkt;
- een test op de clusterkeuze, bijvoorbeeld dat Vault en pgAdmin (`uit`) in `odcn` niet verschijnen en Forgejo alleen in de sandbox;
- guard-tests en snapshot bijgewerkt.

### 5. Een test in de browser

In `tests/e2e/` (VOORSTEL `test_kaart.py`), met de bestaande `app_server` en `auth_page`:

- de tab Kaart van een fixtureproject toont knopen;
- een klik op een component opent geen paneel;
- de "i" opent het paneel;
- een klik op een deployment klapt hem open;
- `/platform` toont de hub.

Neem in de fixtures een tweede project op met een cross-domain-regel naar het eerste, zodat de inkomende koppeling getekend wordt.

### 6. Documentatie

`features/zad-kaart.md` legt uit:

- wat de kaart toont;
- het contract v1, met een verwijzing naar paragraaf 4;
- hoe je het element bouwt (`zad-map:build`) en los draait (`example.html`);
- hoe je `platform.json` bijwerkt;
- hoe je het geheel weer weghaalt.

## 6. Valkuilen

- **ELK en `width: undefined`:** ELK breekt op een compound met `width: undefined` (`Cannot read properties of null (reading 'oe')`). De sleutel moet ontbreken. Dit staat al zo in de spike, in `toElk`.
- **Seed per knoop:** Rough.js tekent bij elke aanroep andere bibberlijnen. Zonder een vaste seed per knoop trilt de kaart bij elke hover.
- **CSS-specificiteit:** de witte achtergrond onder een kaart is `::before`. Een ronde variant (pil, hub) moet even specifiek zijn als de algemene regel, anders blijft er een rechthoek achter staan.
- **Bundelen van lijnen:** `elk.layered.mergeEdges` maakt het onleesbaar welk component welke service gebruikt. Niet aanzetten.
- **Geen eigen lijnen:** teken lijnen niet zelf, routeer ze met ELK. De eigenaar wees zelfgetekende bochten en splines af.
- **Geen geforceerde volgorde:** `forceNodeModelOrder` maakt meer kruisingen. Niet gebruiken.

## 7. Schrijfwegen en poorten

**Er komt geen schrijfweg bij.** Alle nieuwe routes zijn GET en lezen alleen. Er wordt geen veld schrijfbaar, er verandert geen schema, en er gaat niets naar Git of het cluster.

Wat er wél bij komt is **lezen**, en daar staan de poorten zo:

| route | poort | wat het leest |
|---|---|---|
| `/projects/{p}/kaart`, `/projects/{p}/kaart.json` | `PROJECT_READ` via `enforce_route_action` | het eigen project, plus van andere projecten alleen de cross-domain-regels die naar dit project wijzen (naam, component, poort) |
| `/platform`, `/platform.json` | `PORTAL_READ` en de middleware (`is_email_allowed`) | een ingecheckt JSON-bestand, niets per project |

Regels:

- **Geen geheimen in de graaf.** De graafbouwer gebruikt `ProjectSummary.data`, nooit `get_decrypted`. Env-vars, wachtwoorden, attachment-inhoud en tokens komen er niet in.
- **Inkomende koppelingen** tonen van een ander project alleen wat in een cross-domain-regel staat: projectnaam, component en poort. Dat is besloten (besluit 5). Niets anders van dat project.
- **Alleen `html` uit de repo.** Het veld `html` in `info` krijgt alleen markdown uit de repo, via `render_service_help`. Tekst uit een projectfile (namen, omschrijving) gaat altijd als platte tekst, en het element rendert die ge-escaped. Zet een test op een projectomschrijving met `<script>` erin.
- **Links alleen naar het web.** `href` in een rij is alleen een `https://`-adres uit `public_urls_for_deployment`.

## 8. Niet in deze taak

- Het landschap van alle projecten (een admin-view). Dat komt als vervolgkaartje.
- Live status (health, sync) uit ArgoCD op de kaarten.
- Een donker thema: het element zet `color-scheme: light` voor zichzelf.
- De architectuurpagina met Mermaid (`templates_lotc/architecture-overview.html.j2`) blijft zoals hij is.
