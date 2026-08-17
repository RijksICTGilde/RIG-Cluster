# RC-5 — Self-contained service packages

Status: DONE (2026-07-26, HEAD 3392409). Branch: `uniform-declarative-platform-services`, PR #8.

All 12 services migrated to `catalog/<svc>/` packages; component form registry-driven;
`config_models/` + `schemas/services/` dirs removed; `forms/*/fields/services.py` reduced
to the platform-level SERVICES selection field only. Full unit suite 4361 passed (pyright +
ruff clean). Not yet sandbox-validated. Worry #4 (deployment↔attachments cross-ref) was a
false alarm: deployment attachment editables are an independent `DEPLOYMENT_COMP_*` set.

## Doel

Elke service wordt een **package** `opi/services/catalog/<service>/` die ALLES bevat
wat service-specifiek is. Geen service-specifieke definitie leeft nog buiten die map.

Framework-*klassen* (`Editable`, `FormSection`, `EditableVisualizer`, `Sequence`,
`Fieldset`, `WidgetType`) blijven in `opi/forms` en worden geimporteerd — dat is de
bouwdoos, niet de service. Alleen service-specifieke *instances/definities* verhuizen.

### Package-vorm

```
catalog/keycloak/
  __init__.py       # Service-subclass + gedrag  (was catalog/keycloak.py)
  config_model.py   # Pydantic-model            (was services/config_models/keycloak.py)
  editables.py      # Editable-instances        (was forms/editables/fields/services.py deel)
  visualizers.py    # EditableVisualizer        (was forms/visualizers/fields/services.py deel)
  schema.v1.0.json  # gecommit fragment         (was schemas/services/keycloak.v1.0.json)
```

Services zonder config (publish-on-web plain, minio, redis, namespace-redis,
postgresql-database, platform) krijgen alleen `__init__.py`.

## Wat verhuist per service

| Service | config_model | editables | visualizers | schema.json |
|---|---|---|---|---|
| keycloak | ja (+ nested hand-authored) | ja | ja | ja |
| namespace-postgres | ja | ja | ja | ja |
| authorization-wall | ja | ja | ja | ja |
| metrics-scraper | ja | ja (component) | - | ja |
| persistent-storage | ja (gedeeld storage) | ja (component) | - | ja |
| temp-storage | ja (gedeeld storage) | ja (component) | - | ja |
| attachments | ja | ja (component; ook door deployments gebruikt) | - | ? |
| publish-on-web | - | ja (component tls/attachment) | - | - |

## Wrijvingspunten (expliciet oplossen)

1. **Gedeelde storage-config** — `config_models/storage.py` wordt door persistent- en
   temp-storage gedeeld. Blijft een klein gedeeld model. Plaats: `catalog/_shared/storage.py`
   (of een `StorageServiceBase`). Dit is deling *tussen twee services*, geen centrale dump —
   acceptabel en expliciet.
2. **`SERVICES` editable/visualizer** = de service-*keuzelijst*. Platform-niveau, hoort bij
   geen service. Blijft in `forms` (of verhuist naar `catalog/platform/` / registry). Voorstel:
   laten staan in forms; het is de keuzelijst, geen service-config.
3. **Generieke component/project-velden** (naam, image, poorten, resources, path, aliases,
   env-vars) in `fields/components.py` zijn niet service-specifiek → blijven.
4. **deployments -> attachments** — `fields/deployments.py` +
   `visualizers/fields/deployments.py` importeren `ATTACHMENT_USE_SEQUENCE_EDITABLE`. Na verhuizing
   importeren die uit `catalog/attachments/editables.py`. Richting (forms -> catalog) is gelijk aan
   wat de registry al doet; lazy houden waar nodig om cycles te vermijden.
5. **Schema-laadpad** — `config_schema.py::fragment_path` gaat nu naar `schemas/services/`.
   Wordt: pad afgeleid van de service-package (`Service.config_schema_dir` = package-dir). De
   drift-test en de regeneratie-entrypoint blijven byte-identiek werken.
6. **Cirkel-imports** (hoofdrisico) — forms-centrale bestanden importeren dan uit catalog en
   catalog importeert forms-klassen. Bestaande lazy-import-truc (imports binnen methods) behouden;
   catalog-packages mogen op module-load geen forms-*field*-modules of deployments importeren.

## Omgekeerde imports (centrale consumenten via registry)

- `project_registry.py` — stopt met directe import van service-visualizers; haalt ze via registry
  waar nodig. `SERVICES` + generieke velden blijven directe import.
- `wizard_sections.py` — importeert alleen nog `SERVICES` (top-level); service-secties komen al via
  `get_service(t).config_form_section(...)`.
- `fields/deployments.py` / `visualizers/fields/deployments.py` — importeren attachment-editable uit
  het attachments-package.
- `config_schema.py` — fragment-pad per package.

## Fasering (1 service per increment, elk groen + shippable)

- **Fase 0 — scaffolding.** Zet elke `catalog/<svc>.py` om naar `catalog/<svc>/__init__.py`
  (puur verplaatsen, geen inhoud), imports gelijk. Suite groen. `catalog/_shared/` voor storage.
- **Fase 1..N — per service** (volgorde simpel -> complex):
  1. temp-storage (bewijst component-editable + gedeeld storage-model patroon)
  2. persistent-storage
  3. metrics-scraper
  4. authorization-wall (service-editable + visualizer + schema)
  5. namespace-postgres
  6. publish-on-web (alleen component-editables)
  7. attachments (deployments cross-ref)
  8. keycloak (grootste, nested hand-authored) — laatst
  Per service: verplaats config_model + editables + visualizers + schema.json in de package;
  repoint alle importers; verwijder de nu lege stukken uit de gedeelde bestanden.
- **Fase N+1 — opschonen.** Verwijder lege `config_models/`, `schemas/services/`, en de
  service-specifieke resten uit `fields/services.py` + `visualizers/fields/services.py`
  (alleen `SERVICES` + generieke velden blijven). Update `config_schema.py` pad-logica.

## Guardrails

- Bestaande forms-snapshot tests, `test_service_config_schema.py` (drift-lock), all-services e2e.
- Schema-fragmenten byte-identiek na verhuizing (`git mv` + herbevestig met de drift-test).
- Per increment: `ruff check --fix`, `ruff format`, `pyright`, gerichte tests.
- Geen gedragswijziging — puur her-lokaliseren + dependency-richting omdraaien.

## Bewust NIET

- Manager-internals ongemoeid (services blijven dunne adapters).
- `SERVICES` keuzelijst blijft platform-niveau.
- Generieke component/project-editables blijven in `fields/components.py`.
