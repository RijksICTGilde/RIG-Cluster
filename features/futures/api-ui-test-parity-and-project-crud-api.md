# API/UI-testpariteit + cross-testing (en de project-CRUD-API die daarvoor nodig is)

> **Status: PLANNED / not started.** Geparkeerd idee, opgeschreven 2026-08-02. Niet
> gestart. Dit is een testautomatisering-/API-uitbreiding-plan, geen RC-17-werk.

## Context — waarom

Vandaag testen we het meeste end-to-end via de **UI** (Playwright, `tests/e2e/`): de
wizard, de detailpagina, de modals. Wat er nauwelijks is, is een gelijkwaardige set tests
via de **API**, en al helemaal geen **cross-tests** die bewijzen dat een wijziging langs de
ene weg zichtbaar is langs de andere.

Twee dingen die we willen:

1. **Pariteit** — alles wat we in de UI testen, ook via de API testen. De UI en de API zijn
   twee ingangen op dezelfde service-laag; nu dekt de E2E alleen de UI-ingang.
2. **Cross-testing** — als ik iets via de **API** wijzig, controleren dat het ook in de
   **service/UI** zichtbaar is (en omgekeerd). Bijvoorbeeld: een schema toevoegen via
   `PUT .../services/postgresql-database/config/project` en dan op de detailpagina / in het
   projectbestand terugzien; of een component via de UI toevoegen en via de API teruglezen.

Als test-driver willen we hiervoor de **zad-cli** gebruiken (de CLI praat met de API), zodat
de API-tests dezelfde ingang gebruiken als een echte gebruiker/automatisering.

## De blokkade — de API kan nog geen projecten aanmaken/verwijderen

Dit plan kan pas als de API-oppervlakte breder is. Gemeten (2026-08-02):

- **Project aanmaken via de API kan niet.** Er is geen create-project endpoint; aanmaken
  loopt uitsluitend via de wizard (`/forms/wizard`, HTML/HTMX), niet via een JSON-API. Een
  API-/CLI-gedreven test kan dus geen project opzetten om vervolgens tegen te testen.
- **Project verwijderen is synchroon en geen task -- de enige muterende operatie die dat
  is.** Elke andere muterende v2-operatie loopt via een async task (config-writes via
  `_enqueue_config_write`, create/upsert/add-component idem). `DELETE /api/projects/{name}`
  (`router.py:2264`) draait als enige `project_manager.delete_project(...)` **inline** en
  blokkeert tot klaar. De `TaskType`-enum heeft `CREATE_PROJECT` en `DELETE_DEPLOYMENT`, maar
  **geen `DELETE_PROJECT`**. Er is dus geen task-id om op te pollen, en de UI blokkeert op een
  minutenlange delete.

  **Delete async maken is daarmee het fundament van dit plan** (en los te trekken als eigen,
  kleine PR vóór de rest): het maakt delete consistent met alle andere ops, deblokkeert de
  UI, levert de pollbare status voor de "wordt verwijderd"-alert, en is de voorwaarde voor
  een API-/cli-gedreven lifecycle-test.

Voor per-service config bestaat de getypeerde v2-config-API al (zie
`features/service-config-api.md`) -- dat is precies het model dat we projectbreed willen
doortrekken.

## Ontwerp (schets)

1. **Project-CRUD op de API.**
   - `POST /api/v2/projects` — maak een project uit een getypeerd body-model (dezelfde
     validatie als de wizard: `validate_project_schema` + `validate_service_configs`), als
     async `CREATE_PROJECT`-task (die bestaat al voor de wizard-weg; hergebruiken).
   - `DELETE /api/v2/projects/{name}` — als **async `DELETE_PROJECT`-task** i.p.v. synchroon.
     Dat geeft meteen twee dingen: een pollbare task-status, en een niet-blokkerende UI. Dit
     haakt direct aan het "toon 'project wordt verwijderd'"-UX-idee: de detailpagina checkt of
     er een lopende `DELETE_PROJECT`-task voor dit project is en toont dan een eerlijke alert
     i.p.v. half-verwijderde (false) data.
2. **zad-cli als test-driver.** De cli krijgt `project create` / `project delete` bovenop de
   bestaande service-config-commando's, en de API-tests draaien via de cli.
3. **Pariteit-suite.** Voor elke UI-E2E een API-equivalent (maak/lees/wijzig/verwijder via de
   API, verifieer het projectbestand in Forgejo — hetzelfde bewijs-patroon als de UI-E2E's).
4. **Cross-tests.** Een kleine set die bewust van ingang wisselt: API-write → UI/service-read
   en UI-write → API-read, zodat drift tussen de twee ingangen wordt gevangen.

## Next steps (als dit opgepakt wordt)

1. `POST /api/v2/projects` + `DELETE /api/v2/projects/{name}` (async `DELETE_PROJECT`-task)
   ontwerpen en bouwen; body-model = het bestaande `ProjectFileModel`.
2. zad-cli uitbreiden met project create/delete.
3. Een dunne API-lifecycle-test (create → assert in Forgejo → delete → assert weg) als
   fundament, daarna de pariteit-suite laag voor laag.
4. Cross-tests toevoegen op de plekken waar UI en API dezelfde config raken (services,
   componenten, schemas).

## Gerelateerd

- `features/service-config-api.md` — de per-service config-API die het model levert.
- `features/futures/update-add-service-api-for-v2-schema.md` — aanpalende API-uitbreiding.
- Het "project wordt verwijderd"-UX-idee (eerlijke alert i.p.v. false info) hangt aan de
  async `DELETE_PROJECT`-task hierboven.
