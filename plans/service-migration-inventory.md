# Inventarisatie: welke services zijn nog niet klaar voor de nieuwe opzet

Opgemaakt 1 augustus 2026, op basis van het contract in `instructions/services.md` en de echte projectbestanden in `rig-cluster-projects-github/projects` (ongeveer 40 bestanden).

## Hoe hier gemeten is

Een ontbrekende `config_model.py` is op zichzelf géén gat. Het contract zegt letterlijk "Only `__init__.py` is required. A behaviour-only service is a few lines". De juiste toets is dus niet welke bestanden er staan, maar of een service config draagt die niet door een Pydantic-model en een vastgelegd schemafragment gedekt wordt.

Twee eerdere inventarisaties op bestandsnamen gaven een verkeerd beeld: `persistent-storage` en `temp-storage` lijken zonder configmodel omdat er geen `config_model.py` in hun package staat, maar ze delen er een via `catalog/shared/storage.py` en zijn dus compleet. Meet daarom via de registry (`SERVICES`) en niet via `ls`.

De grondwaarheid over wat er werkelijk in projectbestanden staat is opgehaald door alle `services`-lijsten op alle vier de lagen te doorlopen en per service te tellen hoe vaak er een `config`-blok bij zit, plus welke sleutels daarin voorkomen.

## Compleet, hier is niets te doen

| Service | Configmodel | Fragment | Config in echte bestanden |
|---|---|---|---|
| `keycloak` | eigen | ja | project, 9 sleutels waaronder `template`, `restrict-access`, `additional-clients` |
| `authorization-wall` | eigen | ja | project, `banner` |
| `namespace-postgresql-database` | eigen | ja | project, 6 sleutels waaronder `instances`, `storage`, `postInitSQL` |
| `sleep-mode` | eigen | ja | project |
| `metrics-scraper` | eigen | ja | geen configblok in de bestanden, alleen selectie |
| `health-check` | eigen | ja | nog niet in productiebestanden (recent toegevoegd) |
| `persistent-storage` | gedeeld (`shared/storage.py`) | ja | component, lijst van `{name, size, mount-path}`, 17 keer |
| `temp-storage` | gedeeld (`shared/storage.py`) | ja | component, lijst van `{name, size, mount-path}`, 6 keer |

## De vijf echte gaten

Deze services dragen config in echte projectbestanden zonder configmodel en zonder vastgelegd schemafragment. Er is dus niets dat valideert wat erin mag staan, en niets dat drift tegenhoudt.

### 1. `publish-on-web`

Draagt `config: {tls: ...}` op componentniveau in 26 gevallen. Heeft wél `editables.py`, `visualizers.py` en een `config_component_layout()`, dus er is een UI en een vorm, maar geen model en geen fragment. Dit is de kleinste van de vijf: één sleutel met een beperkte waardenreeks, dus een model is snel geschreven. Let op dat de service ook `config_approvals` overschrijft, dus het model moet aansluiten op wat de goedkeuringsweg leest.

### 2. `attachments`

Draagt op componentniveau een lijst, 15 keer, met items in de vorm `{reference, provide-as, path}`. Heeft `editables.py`, `visualizers.py`, `config_component_layout()` én `config_form_section()`, dus de meest uitgebreide UI van de vijf, maar geen model en geen fragment. De configwaarde is een lijst en geen object, dus let bij het modelleren op dat de envelope in `project_v2.json` een `config` als lijst moet blijven toestaan. Waargenomen bijvangst: één deployment-component in de sandbox heeft `attachments: config: []`, een leeg overblijfsel dat in de opruimactie hoort.

### 3. `postgresql-database`

Het grootste geval in aantal: 31 deployment-configblokken met `generation` en `revisions`. Declareert géén configlaag (`config_api_fields` en `config_editables` geven op elke laag niets terug), dus die config wordt buiten de service om gelezen. Het is OPI-beheerde kloonstatus en geen gebruikersinstelling, maar dat maakt het niet minder ongemodelleerd: er is nergens vastgelegd welke vorm die status heeft.

### 4. `minio-storage`

Twee soorten config door elkaar. Op projectniveau `enable-versioning` (1 keer), wat een echte gebruikersinstelling is. Op deploymentniveau `generation` en `revisions` (2 keer), dezelfde OPI-beheerde kloonstatus als bij postgresql-database. Ook hier geen configlaag gedeclareerd. Overweeg de kloonstatus voor beide services in één gedeelde vorm te modelleren, in de geest van `shared/storage.py`.

### 5. `redis`

Draagt `acl-key-prefix` op projectniveau (2 keer). Geen model, geen fragment, geen UI, geen gedeclareerde configlaag. Dat is een gebruikersinstelling zonder enige validatie. Extra aandacht waard omdat er in RIG-World een openstaand punt loopt over precies dat prefix en het scheidingsteken erin, dus de vorm ligt daar niet vrij.

## Twee dingen om te controleren, geen bevestigd gat

- **`namespace-redis`** heeft geen model, geen fragment en geen gedeclareerde configlaag, en er is in de projectbestanden geen enkel configblok voor gevonden. Waarschijnlijk terecht een behaviour-only service, maar dat is niet geverifieerd tegen wat de manager leest.
- **`platform`** is hidden en altijd aan en draagt geen config. Vrijwel zeker terecht leeg.

## Los van de vijf: `config_schema_version` staat overal op `"1.0"`

Ook op de services zonder configmodel: `publish-on-web`, `postgresql-database`, `minio-storage`, `redis`, `namespace-redis`, `platform` en `attachments`. Een versiestempel zonder model is misleidend, want het suggereert een gevalideerd contract dat er niet is. Beslis of dat veld leeg hoort te zijn zolang er geen model is, of dat het zetten ervan juist moet afdwingen dat er een model bestaat. Het tweede is te vangen in `tests/test_service_providers.py`.

## Volgorde die ik zou aanhouden

1. `publish-on-web` en `redis` eerst: één sleutel elk, dus daarmee zet je het patroon neer zonder dat de inhoud in de weg zit.
2. `attachments` daarna: meeste UI, en de lijstvorm van de configwaarde is de eerste echte afwijking.
3. `postgresql-database` en `minio-storage` samen, want ze delen de kloonstatus (`generation`, `revisions`) en die hoort in één gedeelde vorm.
4. `namespace-redis` verifiëren en afsluiten.
5. Pas daarna de `config_schema_version`-regel aanscherpen, want die faalt anders op alles tegelijk.

Stap 1 tot en met 4 zijn onderling onafhankelijk op stap 3 na, dus ze kunnen parallel.

## Per service dezelfde werkwijze

Voor elk van de vijf: `config_model.py` schrijven op basis van wat er feitelijk in de projectbestanden staat (de sleutels hierboven zijn de complete verzameling zoals aangetroffen), `config_model` en `config_schema_version` zetten, `uv run python -m opi.services.config_schema` draaien en het fragment committen, en de configlaag daadwerkelijk declareren in `config_api_fields` en `config_editables` zodat de config via de service loopt en niet via losse `dict.get()` in een manager. Zoek daarbij op waar de manager die config vandaag leest en laat dat via `provider.validate_config(...)` gaan, zoals het contract voorschrijft.

Neem per service ook het `domains:`-patroon over waar een blok van plaats verandert: één functie die bepaalt waar het hoort, waar zowel de migratie als het runtime-schrijfpad doorheen gaat, plus een versie-gebonden migratie én een onvoorwaardelijke reparatie in `_fixup_v2_data`, omdat een al gestempeld bestand de versie-gebonden weg nooit haalt.

## Guardrails na elke service

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```
