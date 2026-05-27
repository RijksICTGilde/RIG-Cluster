# Tenant isolation — open follow-ups

Three items uit de review van PR #70 die buiten scope blijven:

## TOCTOU op concurrente wizard-create

`file_exists` leest de lokale clone; `push_changes` doet bij conflict tot
3x rebase. Twee gelijktijdige creates voor dezelfde projectnaam: beide
passen de bestaanscheck (lokale clone weet nog niets), beide pushen,
tweede push wordt gerebased zonder conflict (verschillende content op
hetzelfde pad) en wint. Last-write wins, stille overname.

Vereist twee SSO-sessies die binnen seconden coördineren — lage
waarschijnlijkheid, wel echt.

Aanbevolen fix: na een gerapporteerde rebase nogmaals `file_exists`
draaien. Als het bestand er nu wel staat, afbreken met dezelfde
foutmelding. Andere opties (advisory lock via ConfigMap, atomic
git-update-ref) zijn robuuster maar veel meer werk; alleen overwegen
als we echte contention zien.

## Hergebruik `check_overwrite_project_file`

`opi/connectors/git.py:1196` heeft al een existence-check helper. PR #70
dupliceert het patroon inline in `simple_background.process_project_background`
en `task_handlers_project.handle_create_project`. Pre-existing
duplicatie, laagprioriteit.

## ValueError → 400 (UX)

`extract_deployment_namespace` raise't `ValueError` bij namespace-
mismatch. De callers (`backup_router`, `restore_router`, `backup_tasks`,
`router_detail_edit`) vangen dat niet, dus FastAPI maakt er een 500
van. Geen info-leak, wel een verwarrende foutmelding voor legitieme
gebruikers met een typo.

Aanbevolen: een `TenantIsolationError` subclass + global exception
handler die hem mapt naar `HTTPException(400, str(e))`.
