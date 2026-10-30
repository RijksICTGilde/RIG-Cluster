# Een restore naar een nieuwe deployment verwijst naar de backup-run

Een backup terugzetten naar een NIEUWE deployment werkt niet. De restore schrijft de onderdelen van de backup-run (`backup_items`) in het `clone-from`-blok van de nieuwe deployment, maar het projectschema kent dat veld niet. De restore slaat het bestand toch op, met de validatie uit, en de verwerking daarna keurt het af. Daarna zit het hele project vast: elke volgende verwerking zakt op hetzelfde blok.

De oplossing: het projectbestand zegt alleen WELKE backup-run er terug moet (`run`), en de managers halen de onderdelen van die run zelf op. Geen runtime-gegevens over Kopia-snapshots in het projectbestand, en geen opslag meer met de validatie uit.

Alle nieuwe namen hieronder zijn een VOORSTEL, tenzij er een codeanker bij staat. Wijk je af, zeg het in de PR.

## Gemeten

Sandbox, 30 oktober 2026, project `tas-hl5`, taak `55d689c7`: terugzetten van run `20261030084847` van deployment `productie` naar een nieuwe deployment `backup`.

1. `_build_and_save_restore_deployment` (`opi/core/backup_tasks.py:130`) zet op de nieuwe deployment:
   ```yaml
   clone-from:
     type: backup
     reference: productie
     mode: once
     backup_items:
       - {snapshot_id: ..., resource_type: database, component_name: mono, reference_name: productie-postgresql, storage_name: null, generation: 0}
       - ...                # bucket en pvc op dezelfde manier
   ```
2. Hij slaat op met `enforce_validation=False` (`opi/core/backup_tasks.py:200`). De log zegt "Persisting project 'tas-hl5' despite a validation failure".
3. De verwerking die volgt (`_provision_deployment_infrastructure`, aangeroepen uit `opi/core/task_handlers_backup.py`) leest het bestand en keurt het af: "Veld 'deployments/1/clone-from': Additional properties are not allowed ('backup_items' was unexpected)". De taak meldt "Infrastructuur aanmaken mislukt voor deployment 'backup'".

`$defs/clone-from` in `opi/schemas/project_v2.json` is een gesloten object met alleen `type`, `reference`, `mode` en `status`, en dat is in de geschiedenis van het schema nooit anders geweest. Dit pad kan dus nooit geslaagd zijn sinds de verwerking tegen het schema toetst.

Productie: geen enkel projectbestand draagt `backup_items` (gecontroleerd op de projects-repo, 30-10-2026). De sandbox is met de hand hersteld.

## Waar `backup_items` nu vandaan komt en heen gaat

- **Bron**: `_gather_backup_runs_async` (`opi/web/router_detail_edit.py:1841`) groepeert de Kopia-snapshots van een deployment per `backup_run_id` (`BackupManager.list_snapshots`, `opi/manager/backup/base.py`, veld `backup_run_id` op regel 103). Per snapshot een item met `snapshot_id`, `resource_type`, `component_name`, `storage_name`, `reference_name`, `generation`.
- **Doorgeven**: de restore-route (`opi/web/router_detail_edit.py:1698-1717`) zet `backup_run_id` EN `backup_items` in de payload van de taak. `handle_restore` (`opi/core/task_handlers_backup.py:236` en verder) gebruikt de items voor de PVC's (`_pre_restore_pvcs`) en geeft ze mee aan `_create_deployment_from_source`, dat ze in het projectbestand zet.
- **Lezers uit het projectbestand**, alle drie op `clone_from.get("backup_items", [])`:
  - `opi/manager/database_manager.py:808` (database)
  - `opi/manager/minio_manager.py:517` (MinIO-bucket)
  - `opi/manager/seaweedfs_manager.py:289` (SeaweedFS-bucket)

## Wat er moet komen

### 1. Het schema: `run` in plaats van `backup_items`

- `$defs/clone-from` krijgt `run`: `{"type": "string"}`, de `backup_run_id` van de run die terug moet. Alleen zinvol bij `type: backup`; leg dat vast in het schema als dat met een `if`/`then` netjes kan, anders in de structurele validatie (`opi/manager/clone_validation.py`), en noem in de PR waar het staat.
- Een nieuwe schemaversie volgens `features/project-schema-versions.md` ("Een migratie toevoegen"): de eerstvolgende vrije op forgejo/main (nu staat main op 2.15). De legacy-patch van de vorige versie haalt `run` weer weg. De migratiestap hoogt alleen de versie op: een bestand met `backup_items` komt de poort niet door (de validatie draait VOOR de migratie), dus een migratie kan het niet repareren. Leg dat in de docstring van de stap uit.

### 2. Eén plek die de items van een run ophaalt

- Haal het groeperen uit `_gather_backup_runs_async` naar een functie die je met (project, cluster, namespace, deployment, run) aanroept en die de items van precies die run teruggeeft. Voorstel: `backup_run_items(...)` in `opi/manager/backup/`. `_gather_backup_runs_async` gebruikt dezelfde groepering, zodat er één definitie is van wat een item is.
- Bron-deployment: `clone-from.reference` (de deployment waarvan de backup is). Namespace en cluster komen van die bron-deployment, zoals `_gather_backup_runs_async` ze nu bepaalt.
- Bestaat de run niet (meer), dan een duidelijke fout met de run-id en de bron-deployment erin, geen lege lijst: anders maakt de restore een lege deployment aan en meldt hij succes.

### 3. De restore schrijft `run`

- `_build_and_save_restore_deployment` zet `"run": <backup_run_id>` in plaats van `"backup_items"`. De taak heeft de run-id al (`payload["backup_run_id"]`).
- `enforce_validation=False` gaat weg op regel 200: het bestand is nu geldig, en een ongeldig bestand hoort de restore te stoppen voordat er iets is aangemaakt.
- Kijk ook naar `enforce_validation=False` op regel 501 (de generatie na een restore van een BESTAANDE deployment). Is daar geen reden meer voor, haal het ook weg; is die er wel, zet hem in een commentaar.

### 4. De lezers halen de items uit de run

- `database_manager.py:808`, `minio_manager.py:517` en `seaweedfs_manager.py:289` lezen niet meer `backup_items` uit `clone-from`, maar roepen de functie uit stap 2 aan met `reference` en `run`.
- `handle_restore` mag de items voor de PVC's uit de payload blijven halen (die zijn er al, het is dezelfde run), of ook via de functie uit stap 2. Kies één en zeg welke.
- `mode: once` met `status.completed` blijft werken zoals nu: na een geslaagde restore wordt de run niet opnieuw opgehaald.

### 5. Tests

- Het bestand dat de restore schrijft valideert tegen het schema, met `run` en zonder `backup_items`. Dit is de test die het oude pad had moeten vangen: laat hem door `validate_project_schema` lopen, niet alleen een dict vergelijken.
- De restore naar een nieuwe deployment loopt door tot en met de verwerking, met een gemockte `BackupManager.list_snapshots`: de database-, bucket- en PVC-items van de run komen bij de juiste manager aan.
- Een `run` die niet bestaat geeft een fout met de run-id erin, en er wordt niets aangemaakt.
- Schema: `run` mag op `clone-from`; een bestand van de vorige versie met `run` wordt geweigerd; `backup_items` blijft geweigerd.
- `_gather_backup_runs_async` geeft dezelfde runs als voor de wijziging (de groepering is alleen verplaatst).

### 6. Documentatie

- `features/backup-restore-wizard.md` (en `features/clone-validatie.md` als die `clone-from` met `type: backup` noemt) beschrijft `clone-from` met `type: backup` en `run`, met een voorbeeld.

## Afspraken

- Nieuwe code (identifiers, testnamen) in het Engels; commentaar, docstrings en docs in het Nederlands.
- Geen em dashes.
- Draai tests gericht en met `--timeout`; geef geen eigen `-m` mee, dat vervangt de `addopts`-selectie uit `pyproject.toml`.

## Buiten scope

- Een restore naar een BESTAANDE deployment: die schrijft geen `clone-from` en werkt al.
- Het opruimen van een deployment die vastzit op `backup_items`: er is er geen in productie, en de sandbox is hersteld.
