# De kaart van ZAD

Een team ziet zijn project als kaart: welke componenten er zijn, welke services ze
gebruiken, welke deployments er draaien en welke koppelingen er met andere projecten
lopen. Wie toegang heeft tot ZAD ziet daarnaast hoe het platform zelf in elkaar zit.

Het bestaat uit twee losse dingen: een **element** in de browser dat een generieke graaf
tekent en niets van ZAD weet, en **graafbouwers in Python** die de ZAD-kennis dragen.

## Wat je ziet

### Projectkaart

`/projects/<naam>/kaart`, het tabblad **Kaart**, achter dezelfde poort als de andere
tabbladen (`PROJECT_READ`). Van links naar rechts:

- het project, het team (klik om de leden te zien) en de projecten die naar dit project
  wijzen;
- de componenten, met geheugen en CPU als staafjes, het health-pad, de schijven, het
  metrics-pad en het aantal bijlagen;
- de services die het project gebruikt, in de kleur van hun categorie;
- het kader **Deployments**: klik een deployment open voor zijn componenten, met de
  image-tag en het webadres;
- helemaal rechts de projecten waar dit project naartoe wijst.

Cross-domain koppelingen staan er in **beide richtingen**, met de naam van het andere
project en component en de poort als label. De inkomende kant komt uit het bestand van de
zender; daarvan wordt niets anders gelezen dan projectnaam, component en poort.

Er staat geen enkel geheim op de kaart. De bouwer leest `ProjectSummary.data` en nooit de
ontsleutelde vorm: geen env-vars, geen wachtwoorden, geen bijlage-inhoud.

### Platformkaart

`/platform`, in het hoofdmenu, voor iedereen die ingelogd is en toegang heeft tot ZAD
(`PORTAL_READ`). Links het kader ZAD met de Operations Manager als hub, rechts de kaders
GitOps, Services voor projecten en Netwerk en DNS, en onderaan de band met wat het cluster
levert. Klik een systeem voor wat het is, de connector die ermee praat, de services die
erop leunen en waar je verder kunt lezen.

De pagina toont alleen het cluster dat deze OPI beheert (`CLUSTER_MANAGER`). Er is geen
schakelaar.

## Het element

`<zad-map src="..."></zad-map>` haalt die URL op (GET, same-origin, met cookies) en tekent
wat er staat. Het staat als één bestand in `operations-manager/python/static/js/zad-map.js`
en gebruikt shadow DOM, dus het neemt niets van de pagina over behalve de NLDD-tokens
(`--semantics-*`, `--primitives-font-family-*`), die door de shadow DOM heen erven.

Attributen:

| attribuut | wat het doet |
|---|---|
| `src` | de URL van de graaf |
| `no-panel` | het element opent zelf geen paneel; het vuurt alleen `zad-map-info` met `detail: { id }` |

Het element vult zijn container, dus de pagina geeft het een hoogte
(`static/css/zad-map.css`).

### Het contract: graaf v1

De volledige beschrijving staat in `opi/services/zad_map/contract.py` als Pydantic-modellen,
en in het plan van RC-403 (paragraaf 4). Kort:

```jsonc
{
  "version": 1,
  "columns": [{ "id": "c0" }, { "id": "c1" }],          // links naar rechts
  "groups": [{ "id": "deps", "title": "Deployments", "column": "c1", "pack": "grid" }],
  "nodes": [
    { "id": "c:editor", "column": "c0", "shape": "card", "color": "component",
      "eyebrow": "single", "title": "editor",
      "rows": [{ "bar": "mem", "req": 121, "lim": 1442, "max": 1640, "text": "1442Mi" }],
      "click": "focus", "info": { "title": "editor", "sections": [] } }
  ],
  "edges": [{ "from": "c:editor", "to": "s:keycloak", "color": "identity" }]
}
```

- **Vormen**: `card`, `pill`, `stack`, `hub`, `frame`, `external`. `state`
  (`elders`, `extern`, `uit`) geeft een stippelrand.
- **Kleuren** zijn namen, geen hex: `component`, `deployment`, `team`, `project`, `web`,
  `identity`, `data`, `ops`, `link`, `gitops`, `network`, `platform`, `external`. Het
  element vertaalt ze naar de Rijkshuisstijl.
- **Klikken**: `focus` dimt alles behalve wat erbij hoort, `expand` klapt open, `info`
  opent meteen het paneel. Een knoop met `info` krijgt een "i"-knop.
- **Alle tekst is platte tekst**, behalve `html` in een infosectie. Dat veld krijgt
  uitsluitend markdown uit de repo (de uitleg van een dienst); tekst uit een projectbestand
  gaat altijd als `text` en wordt ge-escaped.

## Bouwen en los draaien

De bron staat in `operations-manager/zad-map` (Svelte 5, `@xyflow/svelte`, `elkjs`,
`roughjs`). Het resultaat staat in de repo, zoals `codemirror-bundle.js`: er is geen
bouwstap in CI of in de Docker-build.

```bash
task zad-map:build          # npm install + vite build -> static/js/zad-map.js
```

Het gebouwde bestand hoort mee in dezelfde commit als de wijziging in `src/`.

Los draaien, zonder OPI:

```bash
cd operations-manager/zad-map
python3 -m http.server --directory .. 8080
# open http://127.0.0.1:8080/zad-map/example.html
```

`example.html` tekent `example-graph.json`, een voorbeeldgraaf met alle vormen erin.

## platform.json bijwerken

`opi/services/zad_map/platform.json` wordt uit de repo gegenereerd: de bronnen
(`infrastructure/`, `features/`) zitten NIET in het image, dus tijdens het draaien is er
niets anders te lezen.

```bash
cd operations-manager/python
uv run python -m opi.tools.zad_map_platform            # schrijven
uv run python -m opi.tools.zad_map_platform --check    # exit 1 als het achterloopt
```

`tests/test_zad_map_platform.py` faalt als het ingecheckte bestand niet meer overeenkomt met
de repo, met dat commando in de melding. Drie tabellen in de generator zijn met de hand
ingevuld (connector naar systeem, catalogus-service naar systeem, laag per systeem); de
reden staat erbij.

## Afhankelijkheden

- Lezen: `get_project_store()`, `public_urls_for_deployment`,
  `ProjectFileHandler.extract_component_resources`, `merge_rules`/`to_merged_rule`
  (cross-domain) en `render_service_help`. De kaart bouwt niets na.
- De CSP van OPI heeft geen `unsafe-eval`; `tests/test_zad_map_element.py` bewaakt dat de
  bundel er ook geen nodig heeft. ELK draait daarom op de hoofdthread en niet in een worker
  (`worker-src` staat niet open).

## Weghalen

Vier dingen, en niets anders in OPI hangt ervan af:

1. de regel `"kaart"` in `PROJECT_TABS` (`opi/web/lotc_switch.py`) en de regel
   `"Platform"` in `opi/web/menu.py` plus `opi/web/navigation_lotc.py`;
2. de routes `/projects/{p}/kaart(.json)` en `/platform(.json)` in `opi/web/router.py`,
   de tak in `bg/project-tabs.html.j2` en `bg/platform.html.j2`;
3. het pakket `opi/services/zad_map/` en `opi/tools/zad_map_platform.py`;
4. `static/js/zad-map.js`, `static/css/zad-map.css` en `operations-manager/zad-map/`.
